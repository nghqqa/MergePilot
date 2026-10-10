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
import { validateForgeEventV1 } from './forge/index.mjs';
import { createGiteeAdapter } from './forge/gitee.mjs';

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
 * Forge 服务链解析（v1 gitee）：provider_repo_id → active 连接 + 仓库行 + pr 行。
 * 授权面完全来自 DB（连接状态=valid、租户/仓库归属）——payload 的 connection_id/
 * tenant_id 不参与定位（消费者信任服务端解析，不信任事件自报身份）。
 */
export async function resolveForgeServiceContext(pool, { forgeKind, providerRepoId, prNumber,
  instanceId = 'gitee-cloud' }) {
  const b = await pool.query(
    `SELECT fc.connection_id, fc.status, fc.revoked_at, fc.tenant_id,
            rep.repo_id, rep.owner, rep.name
       FROM mu.forge_connection fc
       JOIN mu.repository rep ON rep.tenant_id = fc.tenant_id
            AND rep.forge_instance_id = fc.instance_id AND rep.provider_repo_id = $2
      WHERE fc.instance_id = $1 AND fc.status = 'valid' AND fc.revoked_at IS NULL
        AND rep.provider = $3
      LIMIT 1`,
    [instanceId, String(providerRepoId), forgeKind]);
  const row = b.rows[0];
  if (!row) return { ok: false, reason: 'forge_binding_not_found' };
  const pr = await pool.query(
    `SELECT p.pr_id FROM mu.pull_request p
      WHERE p.tenant_id = $1 AND p.repo_id = $2 AND p.provider_pr_number = $3
      ORDER BY p.updated_at DESC LIMIT 1`,
    [row.tenant_id, row.repo_id, Number(prNumber)]);
  if (!pr.rows.length) return { ok: false, reason: 'pr_snapshot_missing' };
  return { ok: true, tenantId: row.tenant_id, repoId: row.repo_id, prId: pr.rows[0].pr_id,
    owner: row.owner, name: row.name, connectionId: row.connection_id, forgeKind };
}

/**
 * 核心：处理 pull_request 事件（opened/synchronize/reopened 等价；action 仅记档）。
 * 双读分发：payload.schema_version=1 → Forge v1（首版 gitee）；无 schema_version →
 * legacy GitHub 原样（行为不变）。返回 {ok, run, attempt?, findings_count?, completeness?, reason?}；
 * 所有失败路径零副作用残留（run 可能已创建——重复触发幂等复用；BLOCKED 为终性半态，
 * 人工处理后可关）。
 */
export async function handlePullRequestEvent(pool, cfg, { payload, servicePrincipal = 'system:webhook' }) {
  const isForgeV1 = Number(payload?.schema_version ?? 0) === 1;
  let headSha, prNumber, ctx, fetchContext;
  if (isForgeV1) {
    // ── Forge v1（gitee）：消费前契约校验（与入队前同一校验器，#389 教训）──
    const vv = validateForgeEventV1(payload);
    if (!vv.ok) return { ok: false, reason: vv.reason, field: vv.field };
    headSha = String(payload.head_sha);
    prNumber = Number(payload.pr_number);
    ctx = await resolveForgeServiceContext(pool, {
      forgeKind: String(payload.forge_kind), providerRepoId: String(payload.provider_repo_id), prNumber });
    if (!ctx.ok) return { ok: false, reason: ctx.reason };
    // 上下文获取：ForgeAdapter（可注入 cfg.forgeAdapter；默认 env 装配 Gitee 适配器）。
    // GiteeAdapter 返回形状对齐 legacy context 契约（diff/checks/protection/limits），
    // 附加 completeness/files——管线零改动复用。
    const adapter = cfg.forgeAdapter
      ?? createGiteeAdapter({ env: cfg.llmEnv ?? process.env, credentialRef: 'env:MU_GITEE_PAT' });
    fetchContext = async () => adapter.fetchChangeContext({
      providerRepoId: `${ctx.owner}/${ctx.name}`, crKey: String(prNumber),
      expectedHeadSha: headSha, declaredFileCount: payload.declared_file_count ?? null });
  } else {
    // ── legacy GitHub：字段提取与校验原样（契约不变）──
    headSha = String(payload.head_sha ?? '');
    prNumber = Number(payload.pr_number ?? 0);
    const githubRepoId = Number(payload.github_repo_id ?? 0);
    if (!headSha || !prNumber || !githubRepoId) return { ok: false, reason: 'payload_invalid' };
    ctx = await resolveServiceContext(pool, { githubRepoId, prNumber });
    if (!ctx.ok) return { ok: false, reason: ctx.reason };
    fetchContext = () => fetchPrContext(cfg, { installationId: ctx.installationId,
      owner: ctx.owner, repo: ctx.name, prNumber, expectedHeadSha: headSha });
  }

  // ── Wave 3.2：策略解析（创建时一次，随后冻结为 run 快照；运行中不受策略更新影响）──
  const { getAgentPolicy, resolveEffectiveLlmPolicy } = await import('./agent-policy.mjs');
  const agentPolicy = await getAgentPolicy(pool).catch(() => null) ?? { policy_version: 1, mode: 'deterministic_only', enabled: false };
  const effLlm = resolveEffectiveLlmPolicy(agentPolicy, cfg.llmEnv ?? process.env);

  // 幂等建 run（expected_head_sha：synchronize 新 head 天然新 run；快照列随建随冻）
  const { run, created } = await createRunIfAbsent(pool, {
    tenantId: ctx.tenantId, repoId: ctx.repoId, prId: ctx.prId, headSha,
    triggerSource: payload.trigger_source === 'manual' ? 'manual' : 'webhook',
    agentPolicyVersion: Number(agentPolicy.policy_version) || null,
    llmMode: String(agentPolicy.mode ?? 'deterministic_only') });
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
      context = await fetchContext();
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

    // ── Wave 3.1/3.2：可选 LLM 第二阶段审查（使用创建时冻结的有效策略快照 effLlm；
    //    默认 disabled 零网络；失败 fail-closed 回落 deterministic——LLM 建议永不阻断
    //    也永不充当"通过"）──
    // rc.10 SEC-2：真实出站（openai_compatible）统一接入出站治理——与 v2 external
    // reviewer 同一六-reason 否决层（review-arch.evaluateEgressAuthorization）。
    // v1 run 行无冻结 policy 快照（architecture_version=NULL），以当前 policy+consent
    // 运行时装配等价 snapshot（digest=策略体内容哈希）——policy/consent/provider 任一
    // 缺失或非 external_api 档 → EGRESS_DENIED 零网络（生产 evidence_only 恒关闭）。
    // deterministic_mock 零网络不经过此门（如实标记 mock 的测试路径）。
    const { callLlmReviewer, mockLlmReviewer } = await import('./agents/llm.mjs');
    let llmCount = 0, llmCode = null;
    if (effLlm.kind !== 'disabled') {
      const llmClaim = await claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
        provider: effLlm.kind, actorPrincipal: servicePrincipal,
        inputDigest: digestOf(`${headSha}|llm`), maxAttempts: 3, // deterministic 已占 attempt 1——LLM 取 2
        tenantId: ctx.tenantId, repoId: ctx.repoId, prId: ctx.prId, headSha });
      if (llmClaim.ok) {
        const t0 = Date.now();
        let r = null;
        let egressCtx = null;
        if (effLlm.kind !== 'deterministic_mock') {
          // 真实出站前置门：装配运行时等价 snapshot → 六-reason 否决层（deny 零网络）
          const { createReviewPolicyStore } = await import('./review-policy-store.mjs');
          const rpStore = createReviewPolicyStore({ pool });
          const { createEgressAudit } = await import('./agents/egress-audit.mjs');
          const egress = createEgressAudit({ pool });
          // provider 关联键=租户 policy 行声明的 provider_id（v2 控制面唯一权威；
          // 无 policy 行时惰性默认 evidence_only → 必然 deny，与生产 evidence_only 同语义）
          const curPolicy = await rpStore.getPolicy(ctx.tenantId).catch(() => null);
          const providerId = String(curPolicy?.provider_id ?? effLlm.provider ?? effLlm.kind);
          const state = await rpStore.getEgressCurrentState(ctx.tenantId, providerId)
            .catch(() => null);
          const { buildPolicySnapshot } = await import('./review-arch.mjs');
          const rtSnapshot = state?.policy?.review_mode
            ? buildPolicySnapshot({ tenantId: ctx.tenantId, policy: { ...state.policy,
                provider_id: state.policy.provider_id ?? providerId } })
            : null;
          const auth = rtSnapshot
            ? await egress.authorizeEgress(rtSnapshot, state)
            : { authorized: false, reason: 'EGRESS_MODE_NOT_EXTERNAL' };
          if (!auth.authorized) {
            llmCode = `EGRESS_DENIED:${auth.reason}`;
            await finishAttempt(pool, { attemptId: llmClaim.attemptId, status: 'FAILED',
              errorCode: String(llmCode).slice(0, 60), latencyMs: Date.now() - t0,
              evidenceRef: `attempt:${llmClaim.attemptId}` });
            await pool.query(
              `INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
               VALUES ($1,$2,'REVIEW_LLM_EGRESS_DENIED',$3)`,
              [ctx.tenantId, null, JSON.stringify({ run_id: run.run_id,
                reason: auth.reason, provider_id: providerId })]).catch(() => {});
            r = { ok: false, code: llmCode }; // 稳定失败记档——pipeline 继续走 deterministic 结果
          } else {
            egressCtx = { egress, providerId };
          }
        }
        if (!r) {
          try {
            r = effLlm.kind === 'deterministic_mock'
              ? await mockLlmReviewer({ findings })
              : await callLlmReviewer(effLlm, { pr: context.pr, findings, diff: context.diff,
                apiKey: (cfg.llmEnv ?? process.env).MU_LLM_API_KEY },
                { fetchImpl: cfg.llmFetch ?? fetch });
          } catch (e) {
            r = { ok: false, code: `LLM_UNEXPECTED:${String(e?.message ?? '').slice(0, 0) || 'error'}` }; // 无正文
          }
          if (egressCtx) {
            // 出站留痕（成败均记——manifest+digest 零正文；与 v2 external reviewer 同纪律）
            const reqBytes = Buffer.byteLength(JSON.stringify({ pr: context.pr?.title ?? '',
              findings: (findings ?? []).length, diff: context.diff ?? '' }));
            await egressCtx.egress.recordEgress({ tenantId: ctx.tenantId, repoId: ctx.repoId,
              runId: run.run_id, attemptId: llmClaim.attemptId,
              providerId: egressCtx.providerId, modelId: effLlm.model ?? null,
              headSha, diffDigest: digestOf(String(context.diff ?? '')),
              inputDigest: digestOf(`${headSha}|llm`), files: [],
              bytesSent: reqBytes, tokensSent: null,
              redactionsApplied: 0, // 脱敏在 callLlmReviewer 内（maskLine）——计数不可得，如实记 0
              policyVersion: Number(effLlm.policy_version ?? 0),
              consentVersion: effLlm.consent_version ?? null,
              responseDigest: String(r?.outputDigest ?? '').slice(0, 32) || null })
              .catch((e2) => console.error('[mu:egress] recordEgress failed:',
                String(e2?.message ?? e2).slice(0, 120)));
            await pool.query(
              `INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
               VALUES ($1,$2,'REVIEW_LLM_EGRESS',$3)`,
              [ctx.tenantId, null, JSON.stringify({ run_id: run.run_id,
                ok: Boolean(r?.ok), model: effLlm.model ?? null })]).catch(() => {});
          }
        }
        if (r.ok) {
          const mapped = (r.findings ?? []).map((f) => ({
            rule_id: `LLM-${f.category}`, severity: f.severity, confidence: Number(f.confidence ?? 0.5),
            path: String(f.location?.path ?? 'unknown').slice(0, 500),
            line_start: Number(f.location?.line ?? 0) || null, line_end: null,
            title: String(f.summary ?? '').slice(0, 300), evidence_ref: 'llm:citation-required',
            remediation: String(f.recommendation ?? '').slice(0, 1000),
            summary_masked: 'LLM 建议详见 remediation（输出已过 schema 校验）' }));
          llmCount = await insertFindings(pool, { attemptId: llmClaim.attemptId, runId: run.run_id,
            tenantId: ctx.tenantId, repoId: ctx.repoId, prId: ctx.prId, headSha, findings: mapped });
          await finishAttempt(pool, { attemptId: llmClaim.attemptId, status: 'DONE',
            outputDigest: r.outputDigest, modelId: r.model, promptVersion: r.promptVersion,
            latencyMs: r.latencyMs ?? Date.now() - t0, tokenCount: null,
            evidenceRef: `attempt:${llmClaim.attemptId}` });
        } else {
          llmCode = r.code;
          await finishAttempt(pool, { attemptId: llmClaim.attemptId, status: 'FAILED',
            errorCode: String(r.code).slice(0, 60), latencyMs: r.latencyMs ?? Date.now() - t0,
            evidenceRef: `attempt:${llmClaim.attemptId}` });
          // 稳定失败记档（无正文）——pipeline 继续走 deterministic 结果
        }
      }
    }

    return { ok: true, run: await getRun(pool, run.run_id), attempt: claim.attempt,
      findings_count: inserted + llmCount, llm_findings: llmCount,
      llm_code: llmCode ?? (effLlm.kind === 'disabled'
        && ['LLM_POLICY_ENV_MISMATCH', 'LLM_POLICY_MODEL_NOT_ALLOWED'].includes(effLlm.reason)
        ? effLlm.reason : null), // 策略想开但开不成：如实透出（不误报 LLM 已执行）
      stale_head: false, protection: context.protection ?? null,
      completeness: context.completeness ?? null };
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
