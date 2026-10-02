// console/backend/lib/multiuser/agents/external-reviewer.mjs — PR C：external-api 正式 Reviewer。
//
// 链路：Context Builder（PR B）→ 本模块（受控 Provider 调用+findings 持久化）
//  → Leader 消费（confirm/reject/escalate）。
// 安全契约：
//  * 出站前必须过 evaluateEgressAuthorization（egress-audit.authorizeEgress——唯一调用点）；
//  * request envelope 来自 context-builder（双通道 redaction 已应用；本模块不重新构造 payload）；
//  * findings 必须写 mu.agent_finding（source='reviewer'）——禁止只存 output_digest 报成功；
//  * 持久化失败 → attempt FAILED + 死信 + run fail-closed（不进 Leader）；
//  * 零 GitHub 写权限。
import crypto from 'node:crypto';

export const REVIEWER_OUTPUT_SCHEMA = Object.freeze({
  findings_max: 20,
  severity_set: ['P0', 'P1', 'P2', 'P3'],
  keys: ['severity', 'path', 'line_start', 'summary', 'evidence_span'],
});

/** Provider 响应 → 结构化 findings（严格校验；未知键拒绝）。 */
export function parseReviewerOutput(rawText) {
  let parsed;
  try { parsed = JSON.parse(String(rawText ?? '')); }
  catch { return { ok: false, code: 'REVIEWER_OUTPUT_INVALID_JSON' }; }
  if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.findings)) {
    return { ok: false, code: 'REVIEWER_OUTPUT_SCHEMA_INVALID' };
  }
  const out = [];
  for (const f of parsed.findings.slice(0, REVIEWER_OUTPUT_SCHEMA.findings_max)) {
    for (const k of Object.keys(f)) {
      if (!REVIEWER_OUTPUT_SCHEMA.keys.includes(k)) return { ok: false, code: 'REVIEWER_OUTPUT_KEY_FORBIDDEN' };
    }
    if (!REVIEWER_OUTPUT_SCHEMA.severity_set.includes(f.severity)) {
      return { ok: false, code: 'REVIEWER_OUTPUT_SEVERITY_INVALID' };
    }
    if (typeof f.path !== 'string' || !f.path || f.path.length > 500) {
      return { ok: false, code: 'REVIEWER_OUTPUT_PATH_INVALID' };
    }
    out.push({
      rule_id: 'AT-CODE', // external Reviewer 代码级发现（区别 precheck R-*）
      severity: f.severity,
      path: f.path,
      line_start: Number(f.line_start) > 0 ? Number(f.line_start) : null,
      line_end: null,
      title: String(f.summary ?? '').slice(0, 200),
      evidence_ref: String(f.evidence_span ?? '').slice(0, 200) || 'llm:code-context',
      remediation: '', // Reviewer 只发现不修复——Fixer 职责
      summary_masked: String(f.summary ?? '').slice(0, 120),
    });
  }
  return { ok: true, findings: out };
}

/**
 * 主入口（v2 external-api Reviewer 轮次）。
 * deps.fetchImpl 注入（mock 测试零网络；真实 Provider 由 PR G 接入）。
 * 必须在 MU_REVIEW_ARCH=v2 且 snapshot.review_mode=external_api 下调用。
 */
export async function runExternalReviewer(pool, { run, binding, snapshot, context, deps }) {
  const { claimNextAttempt, finishAttempt, insertFindings, moveToDeadLetter, transitionRun } =
    await import('../orchestration.mjs');
  const egress = (await import('./egress-audit.mjs')).createEgressAudit({ pool });
  const rpStore = (await import('../review-policy-store.mjs'))
    .createReviewPolicyStore({ pool });

  // 1) 实时出站授权（唯一调用点——deny 则零网络，run 留 REVIEWED 可重试态）
  const state = await rpStore.getEgressCurrentState(binding.tenantId, snapshot.provider_id);
  const auth = await egress.authorizeEgress(snapshot, state);
  if (!auth.authorized) {
    return { ok: false, stage: 'egress_denied', reason: auth.reason, retryable: true };
  }

  // 2) attempt 领取（provider=external_api——区别 deterministic/agentteams）
  const claim = await claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
    provider: 'external_api', actorPrincipal: 'system:reviewer',
    inputDigest: context.input_digest, maxAttempts: 2,
    tenantId: binding.tenantId, repoId: binding.repoId, prId: binding.prId, headSha: binding.headSha });
  if (!claim.ok) return { ok: false, stage: 'claim_failed', reason: claim.reason };

  // 3) Provider 调用（envelope 已 redacted——本层零重构 payload）
  const t0 = Date.now();
  let responseText = null, responseDigest = null, lastCode = null;
  const f = deps.fetchImpl;
  try {
    const res = await f(`${deps.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${deps.apiKey}` },
      body: JSON.stringify({ model: snapshot.model_id,
        messages: [{ role: 'user', content: context.envelope.serialize() }],
        max_tokens: 4096 }),
    });
    if (res.ok) {
      const j = await res.json();
      responseText = j?.choices?.[0]?.message?.content ?? null;
      responseDigest = crypto.createHash('sha256').update(String(responseText ?? '')).digest('hex').slice(0, 32);
    } else {
      lastCode = `LLM_HTTP_${res.status}`;
    }
  } catch (e) {
    lastCode = String(e?.name ?? '').includes('Abort') ? 'LLM_TIMEOUT' : 'LLM_UNREACHABLE';
  }

  // 4) 出站审计（无论成败——manifest+digest 零正文；重试同 input_digest）
  await egress.recordEgress({ tenantId: binding.tenantId, repoId: binding.repoId,
    runId: run.run_id, attemptId: claim.attemptId,
    providerId: snapshot.provider_id, modelId: snapshot.model_id,
    headSha: binding.headSha, diffDigest: context.binding.diff_digest,
    inputDigest: context.input_digest, files: context.manifest.files,
    bytesSent: Buffer.byteLength(context.envelope.serialize()), tokensSent: context.manifest.tokens_est,
    redactionsApplied: context.manifest.redactions_applied,
    policyVersion: snapshot.policy_version, consentVersion: snapshot.consent_version,
    responseDigest, timeout: lastCode === 'LLM_TIMEOUT', retryCount: 0 });

  if (responseText == null) {
    await finishAttempt(pool, { attemptId: claim.attemptId, status: 'FAILED',
      errorCode: lastCode ?? 'LLM_NO_CONTENT', latencyMs: Date.now() - t0 });
    return { ok: false, stage: 'llm_failed', reason: lastCode ?? 'LLM_NO_CONTENT', retryable: true };
  }

  // 5) 输出解析（严格 schema）
  const parsed = parseReviewerOutput(responseText);
  if (!parsed.ok) {
    await finishAttempt(pool, { attemptId: claim.attemptId, status: 'FAILED',
      errorCode: parsed.code, latencyMs: Date.now() - t0 });
    await moveToDeadLetter(pool, { runId: run.run_id, tenantId: binding.tenantId,
      repoId: binding.repoId, prId: binding.prId, headSha: binding.headSha,
      agentRole: 'reviewer', kind: 'reviewer_output_invalid', reason: parsed.code,
      retryCount: 1, payloadRef: `attempt:${claim.attemptId}` });
    return { ok: false, stage: 'output_invalid', reason: parsed.code };
  }

  // 6) findings 强制持久化（source='reviewer'——禁止只存 digest 的成功路径）
  try {
    await insertFindings(pool, { attemptId: claim.attemptId, runId: run.run_id,
      tenantId: binding.tenantId, repoId: binding.repoId, prId: binding.prId,
      headSha: binding.headSha, findings: parsed.findings });
    // orchestration.insertFindings 的 SQL 无 source 列（默认 'precheck'）——
    // 本 attempt 产出的行显式标记 reviewer（可区分是死端防回归的关键）
    await pool.query(
      `UPDATE mu.agent_finding SET source='reviewer'
        WHERE run_id=$1 AND attempt_id=$2 AND rule_id='AT-CODE'`,
      [run.run_id, claim.attemptId]);
  } catch (e) {
    // 持久化失败 → attempt FAILED + 死信 + fail-closed（不进 Leader）
    await finishAttempt(pool, { attemptId: claim.attemptId, status: 'FAILED',
      errorCode: 'REVIEWER_PERSIST_FAILED', latencyMs: Date.now() - t0 });
    await moveToDeadLetter(pool, { runId: run.run_id, tenantId: binding.tenantId,
      repoId: binding.repoId, prId: binding.prId, headSha: binding.headSha,
      agentRole: 'reviewer', kind: 'reviewer_persist_failed',
      reason: 'REVIEWER_PERSIST_FAILED', retryCount: 1,
      payloadRef: `attempt:${claim.attemptId}` });
    return { ok: false, stage: 'persist_failed', reason: 'REVIEWER_PERSIST_FAILED' };
  }

  await finishAttempt(pool, { attemptId: claim.attemptId, status: 'DONE',
    outputDigest: crypto.createHash('sha256').update(JSON.stringify(parsed.findings)).digest('hex').slice(0, 32),
    modelId: snapshot.model_id, latencyMs: Date.now() - t0,
    evidenceRef: `attempt:${claim.attemptId}` });
  return { ok: true, findings: parsed.findings, attemptId: claim.attemptId,
    inputDigest: context.input_digest, responseDigest };
}

/**
 * Leader 消费（PR C）：precheck + reviewer findings 合并 + confirm/reject/escalate。
 * 冲突规则（ADR-001-r2 §5 / 本任务书五节）：
 *  P2/P3 reviewer reject → 双记录（precheck 行保留 + disposition 拒绝可审计）；
 *  P0/P1 reviewer reject → 双记录 + conflict + 升级人工（禁自动可合并）；
 *  reviewer PASS 不覆盖 P0/P1 冲突 / protection 阻断。
 */
export async function leaderConsumeFindings(pool, { run, binding, protection, servicePrincipal = 'system:leader' }) {
  const { recordDecision, getRun, transitionRun } = await import('../orchestration.mjs');
  const rows = await pool.query(
    `SELECT f.finding_id, f.rule_id, f.severity, f.path, f.line_start, f.summary_masked, f.source
       FROM mu.agent_finding f WHERE f.run_id = $1 ORDER BY f.severity, f.created_at`,
    [run.run_id]);
  const findings = rows.rows ?? [];

  // Reviewer dispositions（若有——本轮无 reviewer 时全 precheck 直通）
  const dispositions = []; // v2: reviewer confirm/reject 由其输出携带（PR C 简化：findings 本身即确认）

  const p0p1 = findings.filter((f) => f.severity === 'P0' || f.severity === 'P1');
  const p2p3 = findings.filter((f) => !['P0', 'P1'].includes(f.severity));

  let decision;
  if (p0p1.length > 0) decision = 'fix_required';
  else if (p2p3.length > 0) decision = 'needs_human';
  else decision = 'clean_complete';

  // protection unknown：审查/建议链可继续，但 merge_eligibility 永不 eligible
  const protectionKnown = protection?.configured === true;

  await recordDecision(pool, { runId: run.run_id, tenantId: binding.tenantId,
    repoId: binding.repoId, prId: binding.prId, headSha: binding.headSha,
    stage: 'leader_decision_after_review', decision,
    rationaleRef: `findings:${findings.length}`, actorPrincipal: servicePrincipal });

  // 独立结果字段（PR A 列）：review_verdict 与 merge_eligibility 分立
  const reviewVerdict = decision === 'clean_complete' ? 'no_blocking_findings'
    : decision === 'fix_required' ? 'changes_requested' : 'inconclusive';
  const mergeEligibility = protectionKnown
    ? (decision === 'clean_complete' ? 'eligible' : 'ineligible')
    : 'unknown'; // unknown→unknown（不冒充 eligible）

  await pool.query(
    `UPDATE mu.review_run SET review_verdict=$2, merge_eligibility=$3,
        review_scope='full_code', execution_mode='external_api'
      WHERE run_id=$1`,
    [run.run_id, reviewVerdict, mergeEligibility]);

  // 状态推进（与既有 leader.advanceAfterReview 兼容：v2 加列不破坏 v1 状态机）
  if (decision === 'clean_complete') {
    await transitionRun(pool, { runId: run.run_id, from: ['REVIEWED'], to: 'COMPLETED' });
  } else if (decision === 'fix_required') {
    await transitionRun(pool, { runId: run.run_id, from: ['REVIEWED'], to: 'FIX_QUEUED' });
  }
  // needs_human：保持 REVIEWED（人工入口）

  return { ok: true, decision, review_verdict: reviewVerdict, merge_eligibility: mergeEligibility,
    findings_total: findings.length, dispositions };
}
