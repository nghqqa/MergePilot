// console/backend/lib/multiuser/agents/egress-audit.mjs — PR B：出站审计与 Provider 调用纪律。
//
// 所有真实 Provider 调用的唯一前置（配合 review-arch.evaluateEgressAuthorization）：
//  1. authorizeEgress(snapshot, state) → deny 则拒（零网络）；
//  2. recordEgress(...) 落 mu.code_egress_event（manifest+digest，零正文）；
//  3. 重试复用同一 input_digest（幂等键=run+attempt+input_digest）；
//  4. run.code_egress 计数 +1（终态审计列）。
import crypto from 'node:crypto';

export function createEgressAudit({ pool }) {
  const q = (t, p) => pool.query(t, p);

  async function authorizeEgress(snapshot, currentState) {
    const { evaluateEgressAuthorization } = await import('../review-arch.mjs');
    return evaluateEgressAuthorization(snapshot, currentState);
  }

  async function recordEgress({ tenantId, repoId, runId, attemptId, providerId, modelId,
      headSha, diffDigest, inputDigest, files, bytesSent, tokensSent, redactionsApplied,
      policyVersion, consentVersion, responseDigest, timeout = false, retryCount = 0 }) {
    await q(
      `INSERT INTO mu.code_egress_event
        (tenant_id, repo_id, run_id, attempt_id, provider_id, model_id,
         head_sha, diff_digest, input_digest, files, bytes_sent, tokens_sent,
         redactions_applied, policy_version, consent_version, response_digest,
         timeout, retry_count)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::text[],$11,$12,$13,$14,$15,$16,$17,$18)`,
      [tenantId, repoId, runId, attemptId, providerId, modelId,
        String(headSha ?? '').slice(0, 40), String(diffDigest ?? '').slice(0, 32),
        String(inputDigest ?? '').slice(0, 32), (files ?? []),
        Number(bytesSent ?? 0), Number(tokensSent ?? 0), Number(redactionsApplied ?? 0),
        Number(policyVersion ?? 0), consentVersion ?? null,
        String(responseDigest ?? '').slice(0, 32), Boolean(timeout), Number(retryCount ?? 0)]);
    // run 计数（additive 列——PR A 建）
    await q(`UPDATE mu.review_run SET code_egress = code_egress + 1 WHERE run_id=$1`, [runId]).catch(() => {});
    return { ok: true };
  }

  return { authorizeEgress, recordEgress };
}

/** mock Provider（PR B 验证专用——零网络；如实标记 mock）。 */
export function mockProviderFetch({ behavior = 'ok' } = {}) {
  const calls = [];
  return {
    calls,
    async fetch(url, opts = {}) {
      calls.push({ url: String(url), body: typeof opts.body === 'string' ? opts.body : null,
        bodyBytes: typeof opts.body === 'string' ? Buffer.byteLength(opts.body) : 0 });
      if (behavior === 'timeout') throw Object.assign(new Error('mock_timeout'), { name: 'AbortError' });
      if (behavior === 'http500') return { ok: false, status: 500, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => ({
        choices: [{ message: { content: JSON.stringify({ findings: [] }) } }] }) };
    },
  };
}

export const responseDigest = (s) => crypto.createHash('sha256').update(String(s ?? '')).digest('hex').slice(0, 32);
