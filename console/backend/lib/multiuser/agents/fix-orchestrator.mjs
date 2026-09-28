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
