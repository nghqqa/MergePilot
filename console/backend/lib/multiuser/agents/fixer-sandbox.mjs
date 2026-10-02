// console/backend/lib/multiuser/agents/fixer-sandbox.mjs — PR D：Fixer sandbox（DRY_RUN patch artifact）。
//
// 契约（任务书五节）：
//  * 只处理 Leader 确认的 finding（run 必须 FIX_QUEUED/FIXING——否则 fail-closed 拒绝）；
//  * 隔离 sandbox（本 PR：进程内沙箱模拟——真实容器沙箱在 PR G 接入点预留）；
//  * 默认 DRY_RUN——绝不写 GitHub（patch 生成 ≠ patch 已应用）；
//  * patch artifact 绑定 head SHA+finding digest+context digest；
//  * patch 生成调用是出站点——必须过 authorizeEgress（唯一调用点）+ recordEgress；
//  * 新 head 到达 → 旧 patch STALE（head_sha 绑定，幂等）。
import crypto from 'node:crypto';

export const FIXER_STATE = Object.freeze({
  generated: 'GENERATED',   // patch 已生成（DRY_RUN）
  stale: 'STALE',          // 新 head 已到——patch 过期
  applied: 'APPLIED',      // 人工应用（当前版本永不自动到达）
});

const digest = (s) => crypto.createHash('sha256').update(String(s ?? '')).digest('hex').slice(0, 32);

/**
 * 生成 DRY_RUN patch artifact。
 * deps.fetchImpl 注入（mock 测试零网络）；出站前 authorizeEgress（与 Reviewer 同一唯一调用点）。
 */
export async function runFixerSandbox(pool, { run, binding, snapshot, findings, context, deps }) {
  const { claimNextAttempt, finishAttempt, getRun, transitionRun } =
    await import('../orchestration.mjs');
  const egress = (await import('./egress-audit.mjs')).createEgressAudit({ pool });
  const rpStore = (await import('../review-policy-store.mjs'))
    .createReviewPolicyStore({ pool });

  // 0) 状态守卫：只在 Leader 决策 fix_required 之后（FIX_QUEUED/FIXING）可跑
  const runRow = await getRun(pool, run.run_id);
  if (!['FIX_QUEUED', 'FIXING'].includes(runRow?.status)) {
    return { ok: false, stage: 'not_fix_required', reason: `RUN_STATE_${runRow?.status ?? 'UNKNOWN'}` };
  }

  // 1) 实时出站授权（patch 生成也把 finding 上下文送出——同 Reviewer 契约）
  const state = await rpStore.getEgressCurrentState(binding.tenantId, snapshot.provider_id);
  const auth = await egress.authorizeEgress(snapshot, state);
  if (!auth.authorized) {
    return { ok: false, stage: 'egress_denied', reason: auth.reason, retryable: true };
  }

  // 2) attempt 领取（provider=external_api）+ 状态推进 FIX_QUEUED→FIXING
  const claim = await claimNextAttempt(pool, { runId: run.run_id, agentRole: 'fixer',
    provider: 'external_api', actorPrincipal: 'system:fixer',
    inputDigest: context?.input_digest ?? digest(JSON.stringify(findings ?? [])),
    maxAttempts: 2, tenantId: binding.tenantId, repoId: binding.repoId,
    prId: binding.prId, headSha: binding.headSha });
  if (!claim.ok) return { ok: false, stage: 'claim_failed', reason: claim.reason };
  if (runRow.status === 'FIX_QUEUED') {
    await transitionRun(pool, { runId: run.run_id, from: ['FIX_QUEUED'], to: 'FIXING' });
  }

  const t0 = Date.now();
  // 3) patch 生成（mock/真实 Provider 都经 fetchImpl——本层不区分）
  const requestText = `Generate a unified diff patch fixing these findings (DRY_RUN only):\n${
    JSON.stringify((findings ?? []).map((f) => ({ severity: f.severity, path: f.path,
      line: f.line_start, issue: f.summary_masked })))}`;
  let patchText = null, lastCode = null;
  try {
    const res = await deps.fetchImpl(`${deps.baseUrl}/chat/completions`, {
      method: 'POST', headers: { 'content-type': 'application/json',
        authorization: `Bearer ${deps.apiKey}` },
      body: JSON.stringify({ model: deps.model,
        messages: [{ role: 'user', content: requestText }],
        max_tokens: 4096 }) });
    if (res.ok) patchText = (await res.json())?.choices?.[0]?.message?.content ?? null;
    else lastCode = `FIXER_HTTP_${res.status}`;
  } catch { lastCode = 'FIXER_UNREACHABLE'; }

  // 4) 出站审计（无论成败——digest 零正文）
  await egress.recordEgress({ tenantId: binding.tenantId, repoId: binding.repoId,
    runId: run.run_id, attemptId: claim.attemptId,
    providerId: snapshot.provider_id, modelId: deps.model,
    headSha: binding.headSha, diffDigest: context?.binding?.diff_digest ?? null,
    inputDigest: context?.input_digest ?? digest(requestText),
    files: (findings ?? []).map((f) => f.path).filter(Boolean),
    bytesSent: Buffer.byteLength(requestText), tokensSent: Math.ceil(requestText.length / 4),
    redactionsApplied: context?.manifest?.redactions_applied ?? 0,
    policyVersion: snapshot.policy_version, consentVersion: snapshot.consent_version,
    responseDigest: patchText != null ? digest(patchText) : null,
    timeout: lastCode === 'FIXER_TIMEOUT', retryCount: 0 });

  if (patchText == null) {
    await finishAttempt(pool, { attemptId: claim.attemptId, status: 'FAILED',
      errorCode: lastCode ?? 'FIXER_NO_CONTENT', latencyMs: Date.now() - t0 });
    return { ok: false, stage: 'fixer_failed', reason: lastCode ?? 'FIXER_NO_CONTENT', retryable: true };
  }

  // 5) patch artifact：绑定 head+findings digest+context digest（新 head→不同 artifact→旧 STALE）
  const findingDigest = digest(JSON.stringify((findings ?? []).map((f) => `${f.rule_id}:${f.path}:${f.line_start}`)));
  const artifact = {
    patch_text: patchText, // 仅内存/测试断言用——库内只存 digest（代码不出审计面）
    patch_digest: digest(patchText),
    head_sha: String(binding.headSha),
    finding_digest: findingDigest,
    context_digest: context?.context_digest ?? null,
    state: FIXER_STATE.generated, // DRY_RUN——永不自动 applied
    generated_at: new Date().toISOString(),
  };

  // 6) 落 mu.fix_attempt（status=DRY_RUN=生成≠应用；库内零 patch 正文）
  await pool.query(
    `INSERT INTO mu.fix_attempt (run_id, tenant_id, repo_id, pr_id, head_sha, attempt, status, patch_digest, artifact_ref, error_code, evidence_ref)
     VALUES ($1,$2,$3,$4,$5,$6,'DRY_RUN',$7,$8,null,$9)`,
    [run.run_id, binding.tenantId, binding.repoId, binding.prId, binding.headSha,
      claim.attempt, artifact.patch_digest,
      `sandbox:${artifact.patch_digest.slice(0, 12)}`, `attempt:${claim.attemptId}`]);
  await finishAttempt(pool, { attemptId: claim.attemptId, status: 'DONE',
    outputDigest: artifact.patch_digest, latencyMs: Date.now() - t0,
    evidenceRef: `attempt:${claim.attemptId}` });

  return { ok: true, artifact, dry_run: true, attemptId: claim.attemptId };
}

/** 新 head 到达 → 标旧 patch STALE（幂等；仅本租户/仓库/PR 范围）。 */
export async function markStalePatches(pool, { tenantId, repoId, prId, newHeadSha }) {
  const r = await pool.query(
    `UPDATE mu.fix_attempt SET status='STALE', updated_at=now()
      WHERE tenant_id=$1 AND repo_id=$2 AND pr_id=$3 AND head_sha<>$4 AND status='DRY_RUN'
      RETURNING fix_id`, [tenantId, repoId, prId, newHeadSha]);
  return { staleCount: r.rows.length };
}
