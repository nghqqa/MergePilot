// console/backend/lib/multiuser/review-service.mjs — Wave 3 PR-B：触发→run→diff→规则审查→落库。
//
// 职责（不含 Leader 编排——PR-C；不含路由接线——PR-E）：
//  * handlePullRequestEvent：payload（webhook 或人工触发等价输入）→ 服务链校验
//    （installation active → repository_binding active → tenant/repo）→ 幂等建 run
//    （expected_head_sha=事件 head）→ 拉 PR 上下文（provider 注入）→ deterministic
//    规则审查 → agent_attempt/agent_finding 落库 → 状态推进至 REVIEWED；
//  * 服务链校验与 resolveEventSyncContext 同构：绝不伪造用户 membership；
//    tenant/repo/pr/head_sha 全部来自服务端解析，payload 只能缩小不能扩大授权面；
//  * token 纪律：provider 返回体不含 token；本模块零日志、零 env 读取；
//  * 幂等：重复 delivery/重复 job → 同一 run（createRunIfAbsent）；已 REVIEWED 的
//    run 重复触发 → 幂等返回既有结果，不重放 attempt；
//  * 失败语义：diff 拉取失败=attempt FAILED（可重试至 maxAttempts）；超限 →
//    dead_letter + run BLOCKED（fail-closed：无法获取输入即不得放行）。
import { createRunIfAbsent, transitionRun, claimNextAttempt, finishAttempt,
  insertFindings, recordDecision, moveToDeadLetter, getRun, digestOf } from './orchestration.mjs';
import { fetchPrContext } from './ghprovider.mjs';
import { reviewDiff, RULES_VERSION } from './reviewer-rules.mjs';

export const REVIEW_SERVICE_VERSION = 'prb-v1';
const MAX_REVIEW_ATTEMPTS = 2;

/** 服务链解析：github_repo_id(+tenant 可选) → binding/installation/tenant/repo/pr 行。 */
export async function resolveServiceContext(pool, { githubRepoId, tenantId = null, prNumber }) {
  const b = await pool.query(
    `SELECT rb.*, i.tenant_id inst_tenant, i.revoked_at, i.suspended_at
       FROM mu.repository_binding rb
       JOIN mu.github_app_installation i ON i.installation_id = rb.installation_id
      WHERE rb.github_repo_id = $1 AND rb.binding_state = 'active'
        ${tenantId ? 'AND rb.tenant_id = $2' : ''}
      LIMIT 1`,
    tenantId ? [String(githubRepoId), tenantId] : [String(githubRepoId)]);
  const row = b.rows[0];
  if (!row) return { ok: false, reason: 'binding_not_found' };
  if (row.revoked_at) return { ok: false, reason: 'installation_revoked' };
  if (row.suspended_at) return { ok: false, reason: 'installation_suspended' };
  const pr = await pool.query(
    `SELECT p.pr_id FROM mu.pull_request p
      WHERE p.tenant_id = $1 AND p.repo_id = $2 AND p.provider_pr_number = $3
      ORDER BY p.updated_at DESC LIMIT 1`,
    [row.tenant_id, row.repo_id, Number(prNumber)]);
  if (!pr.rows.length) return { ok: false, reason: 'pr_snapshot_missing' };
  return { ok: true, tenantId: row.tenant_id, repoId: row.repo_id, prId: pr.rows[0].pr_id,
    installationId: String(row.installation_id), owner: row.owner, name: row.name };
}

/**
 * 核心：处理 pull_request 事件（opened/synchronize/reopened 等价；action 仅记档）。
 * 返回 {ok, run, attempt?, findings_count?, reason?}；所有失败路径零副作用残留
 * （run 可能已创建——重复触发幂等复用；BLOCKED 为终性半态，人工处理后可关）。
 */
export async function handlePullRequestEvent(pool, cfg, { payload, servicePrincipal = 'system:webhook' }) {
  const headSha = String(payload.head_sha ?? '');
  const prNumber = Number(payload.pr_number ?? 0);
  const githubRepoId = Number(payload.github_repo_id ?? 0);
  const action = String(payload.action ?? '');
  if (!headSha || !prNumber || !githubRepoId) return { ok: false, reason: 'payload_invalid' };

  const ctx = await resolveServiceContext(pool, { githubRepoId, prNumber });
  if (!ctx.ok) return { ok: false, reason: ctx.reason };

  // 幂等建 run（expected_head_sha：synchronize 新 head 天然新 run）
  const { run, created } = await createRunIfAbsent(pool, {
    tenantId: ctx.tenantId, repoId: ctx.repoId, prId: ctx.prId, headSha,
    triggerSource: payload.trigger_source === 'manual' ? 'manual' : 'webhook' });
  if (!run) return { ok: false, reason: 'run_create_failed' };

  // 已完成审查的 run：幂等返回（不重放 attempt）
  if (['REVIEWED', 'FIX_QUEUED', 'FIXING', 'VERIFY_QUEUED', 'VERIFYING', 'VERIFIED',
    'COMPLETED', 'BLOCKED', 'FAILED'].includes(run.status)) {
    const existing = await pool.query(
      `SELECT count(*) c FROM mu.agent_finding WHERE run_id = $1`, [run.run_id]);
    return { ok: true, run, idempotent: true, findings_count: Number(existing.rows[0].c) };
  }

  // 推进：RECEIVED→REVIEW_QUEUED→REVIEWING（CAS；输家=并发触发，幂等返回）
  const t1 = await transitionRun(pool, { runId: run.run_id, from: ['RECEIVED'], to: 'REVIEW_QUEUED' });
  const t2 = await transitionRun(pool, { runId: run.run_id, from: ['REVIEW_QUEUED'], to: 'REVIEWING' });
  if (!t1.ok && !t2.ok && !created) {
    const cur = await getRun(pool, run.run_id);
    return { ok: true, run: cur, idempotent: true };
  }

  // attempt：领取（有限重试）
  let lastErr = null;
  for (let round = 0; round < MAX_REVIEW_ATTEMPTS; round++) {
    const claim = await claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
      provider: 'deterministic', actorPrincipal: servicePrincipal,
      inputDigest: digestOf(`${ctx.tenantId}|${ctx.repoId}|${prNumber}|${headSha}`),
      maxAttempts: MAX_REVIEW_ATTEMPTS, tenantId: ctx.tenantId, repoId: ctx.repoId,
      prId: ctx.prId, headSha });
    if (!claim.ok) { lastErr = claim.reason; break; } // max_attempts / conflict → 死信

    const started = Date.now();
    let context;
    try {
      context = await fetchPrContext(cfg, { installationId: ctx.installationId,
        owner: ctx.owner, repo: ctx.name, prNumber, expectedHeadSha: headSha });
    } catch (e) {
      lastErr = `provider_${String(e?.message ?? e).slice(0, 60)}`;
      await finishAttempt(pool, { attemptId: claim.attemptId, status: 'FAILED',
        errorCode: String(e?.message ?? 'provider_error').slice(0, 80),
        latencyMs: Date.now() - started, evidenceRef: `attempt:${claim.attemptId}` });
      continue; // 重试
    }
    if (context.stale_head) {
      // TOCTOU 防护：PR 已推进——本次 attempt 作废，绝不把旧 head 上下文写入本 run
      await finishAttempt(pool, { attemptId: claim.attemptId, status: 'SKIPPED',
        errorCode: 'stale_head', latencyMs: Date.now() - started,
        evidenceRef: `attempt:${claim.attemptId}` });
      await transitionRun(pool, { runId: run.run_id, from: ['REVIEWING'], to: 'BLOCKED' });
      await recordDecision(pool, { runId: run.run_id, tenantId: ctx.tenantId, repoId: ctx.repoId,
        prId: ctx.prId, headSha, stage: 'review_aborted', decision: 'stale_head',
        rationaleRef: `attempt:${claim.attemptId}`, actorPrincipal: servicePrincipal });
      return { ok: false, run: await getRun(pool, run.run_id), reason: 'stale_head' };
    }

    // 规则审查（纯函数）
    const findings = reviewDiff(context.diff, {
      over_diff_limit: context.limits?.over_diff_limit, changed_files: context.pr?.changed_files });
    const outDigest = digestOf(JSON.stringify(findings.map((f) => `${f.rule_id}:${f.path}:${f.line_start}`)));
    await finishAttempt(pool, { attemptId: claim.attemptId, status: 'DONE',
      outputDigest: outDigest, modelId: RULES_VERSION, promptVersion: RULES_VERSION,
      latencyMs: Date.now() - started, evidenceRef: `attempt:${claim.attemptId}` });
    const inserted = await insertFindings(pool, { attemptId: claim.attemptId, runId: run.run_id,
      tenantId: ctx.tenantId, repoId: ctx.repoId, prId: ctx.prId, headSha, findings });
    await transitionRun(pool, { runId: run.run_id, from: ['REVIEWING'], to: 'REVIEWED' });
    await recordDecision(pool, { runId: run.run_id, tenantId: ctx.tenantId, repoId: ctx.repoId,
      prId: ctx.prId, headSha, stage: 'review_done',
      decision: inserted > 0 ? 'findings_present' : 'clean',
      rationaleRef: `attempt:${claim.attemptId}`, actorPrincipal: servicePrincipal });
    return { ok: true, run: await getRun(pool, run.run_id), attempt: claim.attempt,
      findings_count: inserted, stale_head: false, protection: context.protection ?? null };
  }

  // 重试耗尽 → 死信 + BLOCKED（fail-closed：无输入不放行）
  await moveToDeadLetter(pool, { runId: run.run_id, tenantId: ctx.tenantId, repoId: ctx.repoId,
    prId: ctx.prId, headSha, agentRole: 'reviewer', kind: 'review_input_failed',
    reason: String(lastErr ?? 'exhausted').slice(0, 120), retryCount: MAX_REVIEW_ATTEMPTS,
    payloadRef: `evidence://run/${run.run_id}` });
  await transitionRun(pool, { runId: run.run_id, from: ['REVIEWING'], to: 'BLOCKED' });
  await recordDecision(pool, { runId: run.run_id, tenantId: ctx.tenantId, repoId: ctx.repoId,
    prId: ctx.prId, headSha, stage: 'review_blocked', decision: 'input_unavailable',
    rationaleRef: `deadletter:${lastErr ?? ''}`.slice(0, 80), actorPrincipal: servicePrincipal });
  return { ok: false, run: await getRun(pool, run.run_id), reason: `review_input_failed:${lastErr ?? 'exhausted'}` };
}
