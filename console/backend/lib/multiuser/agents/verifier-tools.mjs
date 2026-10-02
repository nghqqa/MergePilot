// console/backend/lib/multiuser/agents/verifier-tools.mjs — PR E：Verifier 独立验证 + test evidence。
//
// 契约（任务书六节）：
//  * 不复用 Reviewer 会话（独立 verifier attempt）；
//  * 输入=finding+patch artifact digest+context digest——缺失/不匹配 fail-closed；
//  * model_judgment 与 test_evidence 分列（verification passed ≠ tests passed）；
//  * 工具白名单（static_check/secret_scan——纯文本静态规则，不执行任意代码）；
//  * 模型判断是出站点——过 authorizeEgress（唯一调用点）+ recordEgress；
//  * 模型不可用 → INCONCLUSIVE 落行（可审计）+ attempt FAILED——不冒充 PASS。
import crypto from 'node:crypto';

export const VERIFIER_TOOLS_ALLOWLIST = Object.freeze([
  'static_check',   // 静态检查（纯文本规则——不执行代码）
  'secret_scan',    // 安全扫描（patch 文本 secret 形状）
]);

// verification_attempt.verdict（大写）→ review_run.verification_verdict（小写 CHECK 域）
const RUN_VERDICT_MAP = Object.freeze({ PASS: 'passed', FAIL: 'failed', BLOCKED: 'inconclusive' });

const digest = (s) => crypto.createHash('sha256').update(String(s ?? '')).digest('hex').slice(0, 32);

/** 静态检查：patch 不应引入新的危险模式。纯文本——不执行代码。 */
export function staticCheck(patchText) {
  const dangerous = [
    /eval\s*\(/i, /child_process/, /exec\s*\(\s*['"`]/, /os\.system/,
    /rm\s+-rf\s+\/(\s|$)/, /chmod\s+777/, /BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY/,
  ];
  const hits = dangerous.filter((re) => re.test(String(patchText ?? '')));
  return { tool: 'static_check', passed: hits.length === 0,
    evidence: hits.length === 0 ? 'no_dangerous_patterns' : `dangerous_pattern:${hits.length}` };
}

/** secret 扫描：patch 文本零 secret 形状。 */
export function secretScan(patchText) {
  const secrets = [/sk-[A-Za-z0-9]{16,}/, /ghp_[A-Za-z0-9]{20,}/, /AKIA[0-9A-Z]{12,}/,
    /password\s*[=:]\s*['"][^'"]{6,}['"]/i];
  const hits = secrets.filter((re) => re.test(String(patchText ?? '')));
  return { tool: 'secret_scan', passed: hits.length === 0,
    evidence: hits.length === 0 ? 'no_secrets' : `secret_shape:${hits.length}` };
}

/**
 * Verifier 主入口。
 * model_judgment（LLM 一致性判断——非测试）与 test_evidence（白名单工具真实执行）
 * 两个域分立落 run 列：verification_verdict / tests_status 互不冒充。
 */
export async function runVerifier(pool, { run, binding, snapshot, findings, patchArtifact, deps }) {
  const { claimNextAttempt, finishAttempt, getRun, transitionRun } =
    await import('../orchestration.mjs');
  const egress = (await import('./egress-audit.mjs')).createEgressAudit({ pool });
  const rpStore = (await import('../review-policy-store.mjs'))
    .createReviewPolicyStore({ pool });

  // 0) 输入完整性 fail-closed：finding/patch artifact 必须齐
  if (!patchArtifact?.patch_digest || !Array.isArray(findings) || !findings.length) {
    return { ok: false, stage: 'verifier_input_missing', reason: 'VERIFIER_INPUT_MISSING' };
  }
  // 0b) 状态守卫：VERIFY_QUEUED/VERIFYING 才可跑（不复用 Reviewer 会话——独立阶段）
  const runRow = await getRun(pool, run.run_id);
  if (!['VERIFY_QUEUED', 'VERIFYING'].includes(runRow?.status)) {
    return { ok: false, stage: 'not_verify_stage', reason: `RUN_STATE_${runRow?.status ?? 'UNKNOWN'}` };
  }
  // 0c) patch_digest 必须对应本 run 的 fix_attempt 行（artifact 绑定校验）
  const fixRow = (await pool.query(
    `SELECT fix_id FROM mu.fix_attempt WHERE run_id=$1 AND patch_digest=$2 ORDER BY attempt DESC LIMIT 1`,
    [run.run_id, patchArtifact.patch_digest])).rows[0];
  if (!fixRow) {
    return { ok: false, stage: 'verifier_input_mismatch', reason: 'PATCH_NOT_OF_THIS_RUN' };
  }

  // 1) 实时出站授权（model_judgment 调用是出站点）
  const state = await rpStore.getEgressCurrentState(binding.tenantId, snapshot.provider_id);
  const auth = await egress.authorizeEgress(snapshot, state);
  if (!auth.authorized) {
    return { ok: false, stage: 'egress_denied', reason: auth.reason, retryable: true };
  }

  // 2) 独立 verifier attempt（不复用 reviewer attempt）
  const inputDigest = digest(patchArtifact.patch_digest + JSON.stringify(findings.map((f) => f.rule_id)));
  const claim = await claimNextAttempt(pool, { runId: run.run_id, agentRole: 'verifier',
    provider: 'external_api', actorPrincipal: 'system:verifier',
    inputDigest, maxAttempts: 2, tenantId: binding.tenantId, repoId: binding.repoId,
    prId: binding.prId, headSha: binding.headSha });
  if (!claim.ok) {
    // 恢复语义（PR G）：重试耗尽 → 死信+run FAILED（fail-closed）
    if (claim.reason === 'max_attempts') {
      const { moveToDeadLetter } = await import('../orchestration.mjs');
      await moveToDeadLetter(pool, { runId: run.run_id, tenantId: binding.tenantId,
        repoId: binding.repoId, prId: binding.prId, headSha: binding.headSha,
        agentRole: 'verifier', kind: 'verifier_max_attempts',
        reason: 'max_attempts', retryCount: 1, payloadRef: `run:${run.run_id}` });
      await transitionRun(pool, { runId: run.run_id,
        from: ['VERIFY_QUEUED', 'VERIFYING'], to: 'FAILED' });
      return { ok: false, stage: 'max_attempts_dead_letter', reason: 'max_attempts' };
    }
    return { ok: false, stage: 'claim_failed', reason: claim.reason };
  }
  if (runRow.status === 'VERIFY_QUEUED') {
    await transitionRun(pool, { runId: run.run_id, from: ['VERIFY_QUEUED'], to: 'VERIFYING' });
  }

  const t0 = Date.now();
  const patchText = String(deps.patchText ?? '');

  // 3) test_evidence 域：白名单工具真实执行（本地零网络——不需出站授权）
  const tools = [staticCheck(patchText), secretScan(patchText)];
  const testsStatus = tools.every((t) => t.passed) ? 'passed' : 'failed';

  // 4) model_judgment 域：LLM 对 patch-finding 一致性判断（非测试）
  const requestText = `Judge whether this patch resolves the findings (consistency only — NOT code testing). `
    + `Patch digest: ${patchArtifact.patch_digest}. Findings: ${
      JSON.stringify(findings.map((f) => ({ severity: f.severity, path: f.path, issue: f.summary_masked })))}. `
    + `Output JSON {"verdict":"PASS"|"FAIL"|"BLOCKED","note":"..."}`;
  let modelJudgment = null, lastCode = null, responseDigest = null;
  try {
    const res = await deps.fetchImpl(`${deps.baseUrl}/chat/completions`, {
      method: 'POST', headers: { 'content-type': 'application/json',
        authorization: `Bearer ${deps.apiKey}` },
      body: JSON.stringify({ model: deps.model,
        messages: [{ role: 'user', content: requestText }], max_tokens: 512 }) });
    if (res.ok) {
      const txt = (await res.json())?.choices?.[0]?.message?.content ?? '';
      responseDigest = digest(txt);
      const j = JSON.parse(txt);
      if (!['PASS', 'FAIL', 'BLOCKED'].includes(j?.verdict)) throw new Error('verdict_invalid');
      modelJudgment = { verdict: j.verdict, note: String(j.note ?? '').slice(0, 500) };
    } else lastCode = `VERIFIER_HTTP_${res.status}`;
  } catch {
    lastCode = lastCode ?? 'VERIFIER_MODEL_UNAVAILABLE';
  }

  // 5) 出站审计（无论成败——digest 零正文）
  await egress.recordEgress({ tenantId: binding.tenantId, repoId: binding.repoId,
    runId: run.run_id, attemptId: claim.attemptId,
    providerId: snapshot.provider_id, modelId: deps.model,
    headSha: binding.headSha, diffDigest: null,
    inputDigest, files: [], bytesSent: Buffer.byteLength(requestText),
    tokensSent: Math.ceil(requestText.length / 4), redactionsApplied: 0,
    policyVersion: snapshot.policy_version, consentVersion: snapshot.consent_version,
    responseDigest, timeout: lastCode === 'VERIFIER_TIMEOUT', retryCount: 0 });

  // 6) 双域落库：verification_attempt（含 INCONCLUSIVE——模型不可用可审计）+ run 两列分立
  const attemptVerdict = modelJudgment?.verdict ?? 'INCONCLUSIVE';
  await pool.query(
    `INSERT INTO mu.verification_attempt
       (run_id, fix_id, tenant_id, repo_id, pr_id, head_sha, attempt, verdict, evidence_ref, error_code)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
    [run.run_id, fixRow.fix_id, binding.tenantId, binding.repoId,
      binding.prId, binding.headSha, claim.attempt, attemptVerdict,
      `tools:${tools.map((t) => `${t.tool}=${t.passed ? 'ok' : 'fail'}`).join(',').slice(0, 200)}`,
      modelJudgment ? null : (lastCode ?? 'VERIFIER_MODEL_UNAVAILABLE')]);
  // run 级：verification_verdict（模型域）/tests_status（工具域）分立——互不冒充
  const runVerdict = RUN_VERDICT_MAP[attemptVerdict] ?? 'inconclusive';
  await pool.query(
    `UPDATE mu.review_run SET verification_verdict=$2, tests_status=$3 WHERE run_id=$1`,
    [run.run_id, runVerdict, testsStatus]);

  await finishAttempt(pool, { attemptId: claim.attemptId,
    status: modelJudgment ? 'DONE' : 'FAILED',
    outputDigest: digest(JSON.stringify({ modelJudgment, tools })),
    errorCode: modelJudgment ? null : (lastCode ?? 'VERIFIER_MODEL_UNAVAILABLE'),
    latencyMs: Date.now() - t0, evidenceRef: `attempt:${claim.attemptId}` });

  // 双域全绿才推进 VERIFIED（任一域未绿不推进）
  if (modelJudgment && runVerdict === 'passed' && testsStatus === 'passed') {
    await transitionRun(pool, { runId: run.run_id, from: ['VERIFYING'], to: 'VERIFIED' });
  }

  return { ok: Boolean(modelJudgment), attempt_verdict: attemptVerdict,
    verification_verdict: runVerdict, tests_status: testsStatus,
    tools, model_judgment: modelJudgment,
    note: 'model_judgment and test_evidence are separate domains' };
}
