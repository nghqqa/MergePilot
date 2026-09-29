// console/backend/lib/multiuser/agents/fix-orchestrator.mjs — Wave 3 PR-D：修复-验证回派循环。
//
// 职责：FIX_QUEUED → Fixer dry-run（fxv-adapter 真子进程）→ fix_attempt 落库 →
// VERIFY_QUEUED → Verifier 独立验证 → verification_attempt 落库 → VERIFIED →
// leader.advanceAfterVerify 终裁（PASS→COMPLETED / FAIL→REWORK 回派 / 超限→BLOCKED+死信）。
// 纪律：默认且唯一修复模式=dry-run；不写 GitHub/不建 commit/不 push；worker 执行前
// 复查 installation/binding（服务链不因重试而放宽）；raw 行文本只在内存流经
// （provider diff → fixer stdin），不落库。
import crypto from 'node:crypto';
import { transitionRun, getRun, recordDecision, moveToDeadLetter, digestOf } from '../orchestration.mjs';
import { retryClaim, finishAttemptOrSkip, advanceAfterVerify, MAX_FIX_ROUNDS } from './leader-helpers.mjs';
import { fixerDryRun, verifierVerify, isForbiddenFixZone } from './fxv-adapter.mjs';
import { fetchPrContext } from '../ghprovider.mjs';
import { parseDiff } from '../reviewer-rules.mjs';

// 每类 finding 的确定性修复规则（pattern=原始行文本；replacement=安全占位改写）
const FIX_RULES = {
  'R-SECRET': (line) => ({ pattern: line, replacement: line.replace(/(ghp_|gho_|sk-)[A-Za-z0-9]+/g, '$1***REDACTED***').replace(/AKIA[0-9A-Z]+/g, 'AKIA***REDACTED***') }),
  'R-SQL-CONCAT': (line) => ({ pattern: line, replacement: '// TODO(security): 参数化查询——PR 审查修复占位' }),
  'R-SHELL-CONCAT': (line) => ({ pattern: line, replacement: '// TODO(security): 数组参数子进程调用——PR 审查修复占位' }),
};
const DEFAULT_FIX = (line) => ({ pattern: line, replacement: '// TODO(security): PR 审查修复占位（人工复核）' });

/**
 * 单轮修复-验证。deps 注入：{repoUrl, testCmd, providerCfg, diffProvider}（测试可控）。
 * 返回 {ok, stage, decision?}。
 */
export async function fixVerifyRound(pool, { run, binding, deps }) {
  const { runId } = { runId: run.run_id };
  const { tenantId, repoId, prId, headSha } = binding;

  // 服务链复查（撤权即拒——不因重试放宽）
  if (deps.assertServiceChain && !(await deps.assertServiceChain())) {
    return { ok: false, stage: 'aborted', reason: 'service_chain_invalid' };
  }

  // ── AgentTeams-first 执行器门控（2026-09-29 原始架构原则）──
  // 三路分发，绝不静默回退 internal：
  //  * rejected（未配置/未知值/internal 无显式降级开关/URL 缺失非法）→ fail-closed：
  //    本轮拒绝执行（run 停留 FIX_QUEUED 可恢复），审计落 executor_gate_rejected；
  //  * agentteams（正式路径）→ 健康检查失败 → fail-closed（同上）；健康通过则
  //    全轮次外部执行，任何失败（含意外异常）落死信+BLOCKED——不回退 internal；
  //  * internal（MU_EXECUTOR=internal + MU_EXECUTOR_INTERNAL_ALLOW 显式声明）→
  //    开发/测试/应急路径（非生产执行器），审计标注 internal_scope。
  const atMod = await import('./agentteams-executor.mjs');
  const atCfg = atMod.resolveAgentTeamsConfig(deps.env ?? process.env);
  const gateAudit = async (kind, detail) => {
    await pool.query(
      `INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
       VALUES ($1, NULL, $2, $3)`,
      [tenantId, kind, JSON.stringify({ run_id: runId, ...detail })]).catch(() => {});
  };
  if (atCfg.kind === 'rejected') {
    await gateAudit('executor_gate_rejected', { reason: atCfg.reason, executor_mode: 'unset_or_invalid' });
    return { ok: false, stage: 'executor_gate_rejected', reason: atCfg.reason };
  }
  if (atCfg.kind === 'agentteams') {
    const health = await atMod.agentTeamsHealthy(atCfg, deps.atFetch);
    if (!health.ok) {
      await gateAudit('executor_gate_rejected', { reason: health.reason, executor_mode: 'agentteams_health_failed' });
      return { ok: false, stage: 'at_health_failed', reason: health.reason };
    }
    let ext;
    try {
      ext = await runExternalRound(pool, atMod, atCfg, { run, binding, deps });
    } catch (e) {
      // 意外异常同样 fail-closed（死信+BLOCKED）——绝不回退 internal
      await moveToDeadLetter(pool, { runId, tenantId, repoId, prId, headSha, agentRole: 'fixer',
        kind: 'at_round_crashed', reason: `AT_ROUND_CRASHED:${String(e?.message ?? e).slice(0, 100)}`,
        retryCount: 0, payloadRef: `run:${runId}` });
      await transitionRun(pool, { runId, from: ['FIXING'], to: 'BLOCKED' }).catch(() => {});
      await transitionRun(pool, { runId, from: ['FIX_QUEUED'], to: 'BLOCKED' }).catch(() => {});
      return { ok: false, stage: 'at_round_crashed', reason: 'AT_ROUND_CRASHED' };
    }
    return ext; // 成功或已落死信的失败——一律返回，不回退
  }
  // internal：显式降级路径（审计标注；生产形态禁用——resolveAgentTeamsConfig 已把关）
  await gateAudit('executor_internal_round', { internal_scope: atCfg.internalScope, run_id: runId });

  // FIX_QUEUED / REWORK_REQUIRED → FIXING（CAS；并发轮次输家幂等退出）
  const fixing = await transitionRun(pool, { runId, from: ['FIX_QUEUED'], to: 'FIXING' }); // 回派先经 requeueFix(REWORK→FIX_QUEUED)，FIXING 只从 FIX_QUEUED 进入（状态机合法边）
  if (!fixing.ok) {
    if (fixing.reason === 'cas_conflict') return { ok: true, stage: 'already_advanced', status: fixing.current };
    return { ok: false, stage: 'fix_transition_failed', reason: fixing.reason };
  }

  // fixer attempt
  const fixClaim = await retryClaim(pool, { runId, agentRole: 'fixer', provider: 'fxv',
    actorPrincipal: 'system:leader', inputDigest: digestOf(`${runId}|fix`),
    maxAttempts: MAX_FIX_ROUNDS * 2, tenantId, repoId, prId, headSha });
  if (!fixClaim.ok) return { ok: false, stage: 'fix_claim_failed', reason: fixClaim.reason };

  // 取待修复 P0/P1 finding（首个非禁改区）
  const fRows = await pool.query(
    `SELECT rule_id, path, line_start, severity FROM mu.agent_finding
      WHERE run_id=$1 AND severity IN ('P0','P1') ORDER BY severity, created_at LIMIT 1`, [runId]);
  const finding = fRows.rows[0] ?? null;
  let fixRow = null;
  if (!finding || isForbiddenFixZone(finding.path)) {
    await pool.query(
      `INSERT INTO mu.fix_attempt (run_id, tenant_id, repo_id, pr_id, head_sha, attempt, status, error_code, evidence_ref)
       VALUES ($1,$2,$3,$4,$5,$6,'SKIPPED',$7,$8)`,
      [runId, tenantId, repoId, prId, headSha, fixClaim.attempt,
        finding ? 'forbidden_zone' : 'no_actionable_finding', `attempt:${fixClaim.attemptId}`]);
  } else {
    // 重新拉 diff（内存流：raw 行 → fixer stdin；不落库）
    let rawLine = null;
    try {
      const ctx = await fetchPrContext(deps.providerCfg, { installationId: deps.installationId,
        owner: deps.owner, repo: deps.repoName, prNumber: deps.prNumber, expectedHeadSha: headSha });
      if (ctx.stale_head) throw new Error('stale_head');
      const files = parseDiff(ctx.diff);
      const f = files.find((x) => x.path === finding.path && x.added.some((l) => l.line === finding.line_start));
      rawLine = f?.added.find((l) => l.line === finding.line_start)?.text ?? null;
    } catch (e) {
      await finishAttemptOrSkip(pool, fixClaim.attemptId, 'FAILED', String(e?.message ?? 'diff_refetch_failed').slice(0, 80));
      await moveToDeadLetter(pool, { runId, tenantId, repoId, prId, headSha, agentRole: 'fixer',
        kind: 'fix_input_failed', reason: 'diff_refetch_failed', retryCount: fixClaim.attempt,
        payloadRef: `evidence://run/${runId}` });
      await transitionRun(pool, { runId, from: ['FIXING'], to: 'BLOCKED' });
      return { ok: false, stage: 'fix_input_failed' };
    }
    const rule = (FIX_RULES[finding.rule_id] ?? DEFAULT_FIX)(rawLine ?? '');
    let fix;
    try {
      fix = await fixerDryRun({ repoUrl: deps.repoUrl, baseHeadSha: headSha,
        finding: { file: finding.path, pattern: rule.pattern, replacement: rule.replacement } });
    } catch (e) {
      // worker 崩溃/超时（如 head_sha 在仓库不可达）——按 FAILED 记档，不裸抛
      fix = { ok: false, reason: `fixer_crashed:${String(e?.message ?? e).slice(0, 60)}` };
    }
    const status = fix.ok ? 'DRY_RUN' : 'FAILED';
    const ins = await pool.query(
      `INSERT INTO mu.fix_attempt (run_id, tenant_id, repo_id, pr_id, head_sha, attempt, status,
          patch_digest, artifact_ref, error_code, evidence_ref)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING fix_id, status, patch_digest`,
      [runId, tenantId, repoId, prId, headSha, fixClaim.attempt, status,
        fix.ok ? fix.patch_digest : null, fix.ok ? `fxv-dryrun:${fix.patch_digest?.slice(0, 16)}` : null,
        fix.ok ? null : String(fix.reason ?? 'fixer_failed').slice(0, 80),
        `attempt:${fixClaim.attemptId}`]);
    fixRow = ins.rows[0];
    await finishAttemptOrSkip(pool, fixClaim.attemptId, fix.ok ? 'DONE' : 'FAILED',
      fix.ok ? null : String(fix.reason ?? 'fixer_failed'));
  }

  // VERIFY 阶段
  await transitionRun(pool, { runId, from: ['FIXING'], to: 'VERIFY_QUEUED' });
  await transitionRun(pool, { runId, from: ['VERIFY_QUEUED'], to: 'VERIFYING' });
  const verClaim = await retryClaim(pool, { runId, agentRole: 'verifier', provider: 'fxv',
    actorPrincipal: 'system:leader', inputDigest: digestOf(`${runId}|verify`),
    maxAttempts: MAX_FIX_ROUNDS * 2, tenantId, repoId, prId, headSha });
  if (!verClaim.ok) return { ok: false, stage: 'verify_claim_failed', reason: verClaim.reason };

  let verdict = 'BLOCKED', evidence = { error: 'no_patch' };
  const latestFix = fixRow ?? (await pool.query(
    `SELECT fix_id, patch_digest, status FROM mu.fix_attempt WHERE run_id=$1 ORDER BY attempt DESC LIMIT 1`,
    [runId])).rows[0];
  if (latestFix?.status === 'DRY_RUN' && latestFix.patch_digest) {
    // 重新生成 patch 文本供 verifier（deterministic：同输入同输出——重新 dry-run 一次）
    const fRows2 = await pool.query(
      `SELECT rule_id, path, line_start FROM mu.agent_finding
        WHERE run_id=$1 AND severity IN ('P0','P1') ORDER BY severity, created_at LIMIT 1`, [runId]);
    const fd = fRows2.rows[0];
    let rawLine2 = null;
    const ctx2 = await fetchPrContext(deps.providerCfg, { installationId: deps.installationId,
      owner: deps.owner, repo: deps.repoName, prNumber: deps.prNumber, expectedHeadSha: headSha })
      .catch(() => null);
    if (ctx2 && !ctx2.stale_head) {
      const files2 = parseDiff(ctx2.diff);
      rawLine2 = files2.find((x) => x.path === fd?.path)?.added
        .find((l) => l.line === fd?.line_start)?.text ?? null;
      if (rawLine2 && fd) {
        const rule2 = (FIX_RULES[fd.rule_id] ?? DEFAULT_FIX)(rawLine2);
        let fix2 = null;
        try {
          fix2 = await fixerDryRun({ repoUrl: deps.repoUrl, baseHeadSha: headSha,
            finding: { file: fd.path, pattern: rule2.pattern, replacement: rule2.replacement } });
        } catch { fix2 = { ok: false }; }
        if (fix2.ok) {
          const v = await verifierVerify({ repoUrl: deps.repoUrl, baseHeadSha: headSha,
            patchText: fix2.patch_text, patchDigest: fix2.patch_digest, testCmd: deps.testCmd });
          verdict = v.verdict; evidence = v.evidence;
        }
      }
    }
  }
  await pool.query(
    `INSERT INTO mu.verification_attempt (run_id, fix_id, tenant_id, repo_id, pr_id, head_sha, attempt, verdict, evidence_ref, error_code)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
    [runId, latestFix?.fix_id ?? crypto.randomUUID(), tenantId, repoId, prId, headSha,
      verClaim.attempt, verdict, `attempt:${verClaim.attemptId}`,
      verdict === 'BLOCKED' ? String(evidence?.error ?? 'blocked').slice(0, 80) : null]);
  await finishAttemptOrSkip(pool, verClaim.attemptId, 'DONE');
  await transitionRun(pool, { runId, from: ['VERIFYING'], to: 'VERIFIED' });

  const final = await advanceAfterVerify(pool, { runId, tenantId, repoId, prId, headSha,
    verdict, unresolvedP0P1: [], fixAttemptNo: fixClaim.attempt });
  return { ok: true, stage: 'verified', verdict, decision: final.decision };
}

// ── Wave 3.3：外部 AgentTeams 轮次（fix+verify 交给外部四 Agent；MergePilot 终裁）──
async function runExternalRound(pool, atMod, atCfg, { run, binding, deps }) {
  const { runId } = { runId: run.run_id };
  const { tenantId, repoId, prId, headSha } = binding;
  const fetchImpl = deps.atFetch ?? fetch;
  // 幂等建四 Agent（merge-patch；不暴露端口/无 MCP/无凭据注入）
  const ensured = await atMod.ensureFourAgents(atCfg, { fetchImpl });
  if (!ensured.ok) return { ok: false, stage: 'at_ensure_failed', reason: ensured.reason };
  // 取 P0/P1 finding 摘要（脱敏出站）
  const fRows = await pool.query(
    `SELECT rule_id, severity, path, line_start, summary_masked FROM mu.agent_finding
      WHERE run_id=$1 AND severity IN ('P0','P1') ORDER BY severity, created_at LIMIT 20`, [runId]);
  const submitted = await atMod.submitExternalRound(atCfg, { runId, findings: fRows.rows, fetchImpl });
  if (!submitted.ok) return { ok: false, stage: 'at_submit_failed', reason: submitted.reason };
  const proj = submitted.project_id;
  // 状态迁移与 attempt 记录（provider=agentteams）
  await transitionRun(pool, { runId, from: ['FIX_QUEUED'], to: 'FIXING' });
  const fixClaim = await retryClaim(pool, { runId, agentRole: 'fixer', provider: 'agentteams',
    actorPrincipal: 'system:leader', inputDigest: digestOf(`${runId}|at-fix`),
    maxAttempts: MAX_FIX_ROUNDS * 2, tenantId, repoId, prId, headSha });
  const polled = await atMod.pollExternalRound(atCfg, { projectId: proj, fetchImpl,
    maxWaitMs: atCfg.timeout });
  if (!polled.ok) {
    await atMod.cancelExternalRound(atCfg, { projectId: proj, taskId: 't-fix',
      reason: 'mergepilot-poll-failed', submissionId: proj, fetchImpl }).catch(() => {});
    if (fixClaim.ok) await finishAttemptOrSkip(pool, fixClaim.attemptId, 'FAILED', polled.reason);
    await moveToDeadLetter(pool, { runId, tenantId, repoId, prId, headSha, agentRole: 'fixer',
      kind: 'at_round_failed', reason: String(polled.reason).slice(0, 120), retryCount: fixClaim.ok ? fixClaim.attempt : 0,
      payloadRef: `at:${atMod.atDigest(proj)}` });
    await transitionRun(pool, { runId, from: ['FIXING'], to: 'BLOCKED' });
    return { ok: false, stage: 'at_poll_failed', reason: polled.reason };
  }
  // Fixer 结果（建议文本——不应用；只校验形状并留 evidence 摘要）
  const fixRes = await atMod.fetchTaskSummary(atCfg, { projectId: proj, taskId: 't-fix', fetchImpl });
  let fixHintOk = false;
  if (fixRes.ok) {
    try {
      const parsed = JSON.parse(fixRes.summary);
      fixHintOk = atMod.validateAgentTeamsOutput('fixer', parsed).ok;
    } catch { fixHintOk = false; }
  }
  await pool.query(
    `INSERT INTO mu.fix_attempt (run_id, tenant_id, repo_id, pr_id, head_sha, attempt, status,
        patch_digest, artifact_ref, error_code, evidence_ref)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING fix_id`,
    [runId, tenantId, repoId, prId, headSha, fixClaim.ok ? fixClaim.attempt : 1,
      fixHintOk ? 'DRY_RUN' : 'FAILED', fixHintOk ? digestOf(fixRes.summary) : null,
      fixHintOk ? `at:${atMod.atDigest(proj)}` : null,
      fixHintOk ? null : 'at_fix_output_invalid', fixClaim.ok ? `attempt:${fixClaim.attemptId}` : 'at']);
  if (fixClaim.ok) await finishAttemptOrSkip(pool, fixClaim.attemptId, fixHintOk ? 'DONE' : 'FAILED',
    fixHintOk ? null : 'at_fix_output_invalid');
  // Verifier 结果（外部独立验证；MergePilot 仍做 schema 校验+策略终裁）
  await transitionRun(pool, { runId, from: ['FIXING'], to: 'VERIFY_QUEUED' });
  await transitionRun(pool, { runId, from: ['VERIFY_QUEUED'], to: 'VERIFYING' });
  const verClaim = await retryClaim(pool, { runId, agentRole: 'verifier', provider: 'agentteams',
    actorPrincipal: 'system:leader', inputDigest: digestOf(`${runId}|at-verify`),
    maxAttempts: MAX_FIX_ROUNDS * 2, tenantId, repoId, prId, headSha });
  const verRes = await atMod.fetchTaskSummary(atCfg, { projectId: proj, taskId: 't-verify', fetchImpl });
  let verdict = 'BLOCKED', verCode = 'at_verify_output_invalid';
  if (verRes.ok) {
    try {
      const parsed = JSON.parse(verRes.summary);
      const v = atMod.validateAgentTeamsOutput('verifier', parsed);
      if (v.ok) { verdict = parsed.verdict; verCode = null; }
    } catch { /* 保持 BLOCKED */ }
  }
  await pool.query(
    `INSERT INTO mu.verification_attempt (run_id, fix_id, tenant_id, repo_id, pr_id, head_sha, attempt, verdict, evidence_ref, error_code)
     SELECT $1, (SELECT fix_id FROM mu.fix_attempt WHERE run_id=$1 ORDER BY attempt DESC LIMIT 1), $2,$3,$4,$5,$6,$7,$8,$9`,
    [runId, tenantId, repoId, prId, headSha, verClaim.ok ? verClaim.attempt : 1, verdict,
      verClaim.ok ? `attempt:${verClaim.attemptId}` : 'at', verCode]);
  if (verClaim.ok) await finishAttemptOrSkip(pool, verClaim.attemptId, 'DONE');
  await transitionRun(pool, { runId, from: ['VERIFYING'], to: 'VERIFIED' });
  const final = await advanceAfterVerify(pool, { runId, tenantId, repoId, prId, headSha,
    verdict, unresolvedP0P1: [], fixAttemptNo: fixClaim.ok ? fixClaim.attempt : 1 });
  return { ok: true, stage: 'at_verified', verdict, decision: final.decision, executor: 'agentteams' };
}
