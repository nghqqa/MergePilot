// console/backend/lib/multiuser/agents/leader.mjs — Wave 3 PR-C：Leader 编排与策略。
//
// Leader 是确定性策略引擎（非 LLM——LLM 不得编排/不得自动批准）：
//  * review 阶段完成后按策略裁定：clean→COMPLETED / 需修复→FIX_QUEUED / 需人工→
//    保持 REVIEWED+decision=needs_human / 保护状态未知→BLOCKED（fail-closed）；
//  * verify 阶段后终裁（PR-D 接执行，此处定义协议）：PASS→COMPLETED，FAIL→
//    REWORK_REQUIRED（回派 Fixer，attempt 递增有界），超限→BLOCKED+死信；
//  * VERIFIED→REWORK_REQUIRED 唯一允许场景（PR-A P2-2 定义）：verify PASS 后、
//    终裁前复查发现新的未解决 P0/P1 finding（并发写入），否则不得回退；
//  * claimNextAttempt 的 attempt_conflict 由 retryClaim 有界重试吸收（PR-A P2-1）；
//  * 全程传播绑定五元组；orchestration_decision 记录每一步裁定。
import { transitionRun, getRun, claimNextAttempt, recordDecision, moveToDeadLetter } from '../orchestration.mjs';

export const LEADER_POLICY_VERSION = 'leader-v1';
export const MAX_FIX_ROUNDS = 2;

/** attempt 领取有界重试（吸收罕见的 attempt_conflict 竞态——PR-A P2-1 修复）。 */
export async function retryClaim(pool, args, { retries = 3, claimImpl = null } = {}) {
  const claim = claimImpl ?? ((a) => claimNextAttempt(pool, a));
  let last = null;
  for (let i = 0; i <= retries; i++) {
    const r = await claim(args);
    if (r.ok || r.reason === 'max_attempts') return r;
    last = r.reason;
  }
  return { ok: false, reason: `claim_retry_exhausted:${last}` };
}

/**
 * 策略裁定（纯函数）：review 结果 → 下一步。
 * 输入：findings（含 severity）、protection（branch protection 探测结果）。
 * 规则（任务书 §六 默认策略）：
 *  1. protection 未知/未配置 → BLOCKED（fail-closed，无论 findings）；
 *  2. P0/P1 存在 → fix_required（不得自动批准；修复走 PR-D 循环）；
 *  3. 仅 P2/P3 → needs_human（人工裁量）；
 *  4. clean → clean_complete。
 */
export function decideAfterReview({ findings, protection }) {
  if (!protection || protection.configured !== true) {
    return { decision: 'blocked', reason: 'branch_protection_unknown' };
  }
  const sev = (findings ?? []).map((f) => f.severity);
  if (sev.some((s) => s === 'P0' || s === 'P1')) return { decision: 'fix_required' };
  if (sev.length > 0) return { decision: 'needs_human' };
  return { decision: 'clean_complete' };
}

/**
 * Review 完成后的 Leader 推进：REVIEWED → FIX_QUEUED / COMPLETED / BLOCKED。
 * needs_human：保持 REVIEWED（人工入口在 PR-E UI），decision 记档。
 */
export async function advanceAfterReview(pool, { runId, tenantId, repoId, prId, headSha,
  findings, protection, principal = 'system:leader' }) {
  const run = await getRun(pool, runId);
  if (!run) return { ok: false, reason: 'not_found' };
  const pol = decideAfterReview({ findings, protection });
  const dec = await recordDecision(pool, { runId, tenantId, repoId, prId, headSha,
    stage: 'leader_decision_after_review', decision: pol.decision,
    rationaleRef: pol.reason ?? '', actorPrincipal: principal, policyVersion: LEADER_POLICY_VERSION });
  if (pol.decision === 'clean_complete') {
    const t = await transitionRun(pool, { runId, from: ['REVIEWED'], to: 'COMPLETED' });
    return { ok: t.ok, decision: pol.decision, transition: t.ok ? 'COMPLETED' : t.reason };
  }
  if (pol.decision === 'blocked') {
    const t = await transitionRun(pool, { runId, from: ['REVIEWED'], to: 'BLOCKED' });
    return { ok: t.ok, decision: pol.decision, reason: pol.reason };
  }
  if (pol.decision === 'fix_required') {
    const t = await transitionRun(pool, { runId, from: ['REVIEWED'], to: 'FIX_QUEUED' });
    return { ok: t.ok, decision: pol.decision, transition: t.ok ? 'FIX_QUEUED' : t.reason };
  }
  return { ok: true, decision: 'needs_human', decision_id: dec }; // 保持 REVIEWED
}

/**
 * Verify 结果后的 Leader 终裁（PR-D 执行器调用）：
 *  PASS → 复查 P0/P1：全部解决 → COMPLETED；存在未解决 → REWORK_REQUIRED（P2-2 场景）
 *  FAIL → REWORK_REQUIRED（回派，fixAttemptNo 超限 → BLOCKED + 死信）
 *  BLOCKED → BLOCKED
 */
export async function advanceAfterVerify(pool, { runId, tenantId, repoId, prId, headSha,
  verdict, unresolvedP0P1 = [], fixAttemptNo, principal = 'system:leader' }) {
  const run = await getRun(pool, runId);
  if (!run) return { ok: false, reason: 'not_found' };
  if (verdict === 'BLOCKED') {
    await recordDecision(pool, { runId, tenantId, repoId, prId, headSha,
      stage: 'leader_final', decision: 'blocked', rationaleRef: 'verifier_blocked',
      actorPrincipal: principal, policyVersion: LEADER_POLICY_VERSION });
    const t = await transitionRun(pool, { runId, from: ['VERIFIED'], to: 'BLOCKED' });
    return { ok: t.ok, decision: 'blocked' };
  }
  if (verdict === 'PASS' && (!unresolvedP0P1 || unresolvedP0P1.length === 0)) {
    await recordDecision(pool, { runId, tenantId, repoId, prId, headSha,
      stage: 'leader_final', decision: 'verified_complete', rationaleRef: 'verifier_pass',
      actorPrincipal: principal, policyVersion: LEADER_POLICY_VERSION });
    const t = await transitionRun(pool, { runId, from: ['VERIFIED'], to: 'COMPLETED' });
    return { ok: t.ok, decision: 'completed' };
  }
  // FAIL，或 PASS 但存在未解决 P0/P1（唯一允许的 VERIFIED→REWORK 场景）
  if (Number(fixAttemptNo) >= MAX_FIX_ROUNDS) {
    await moveToDeadLetter(pool, { runId, tenantId, repoId, prId, headSha, agentRole: 'fixer',
      kind: 'fix_rounds_exhausted', reason: `max=${MAX_FIX_ROUNDS}`, retryCount: Number(fixAttemptNo),
      payloadRef: `evidence://run/${runId}` });
    await recordDecision(pool, { runId, tenantId, repoId, prId, headSha,
      stage: 'leader_final', decision: 'blocked', rationaleRef: 'fix_rounds_exhausted',
      actorPrincipal: principal, policyVersion: LEADER_POLICY_VERSION });
    const t = await transitionRun(pool, { runId, from: ['VERIFIED'], to: 'BLOCKED' });
    return { ok: t.ok, decision: 'blocked_exhausted' };
  }
  await recordDecision(pool, { runId, tenantId, repoId, prId, headSha,
    stage: 'leader_rework', decision: 'rework_required',
    rationaleRef: verdict === 'PASS' ? 'post_verify_p0p1_unresolved' : 'verifier_fail',
    actorPrincipal: principal, policyVersion: LEADER_POLICY_VERSION });
  const t = await transitionRun(pool, { runId, from: ['VERIFIED'], to: 'REWORK_REQUIRED' });
  return { ok: t.ok, decision: 'rework' };
}

/** REWORK_REQUIRED → FIX_QUEUED（回派入口；PR-D 循环调用）。 */
export async function requeueFix(pool, { runId, ...binding }) {
  return transitionRun(pool, { runId, from: ['REWORK_REQUIRED'], to: 'FIX_QUEUED' });
}
