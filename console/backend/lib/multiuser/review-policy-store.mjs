// console/backend/lib/multiuser/review-policy-store.mjs — ADR-002 PR A：策略/Provider/consent 存取层。
//
// 纪律：
//  * 零凭据列（写入前 validateReviewPolicy + secret 形状扫描双保险）；
//  * 策略更新=单事务 CAS（policy_version 递增）+ revision append-only（不可物理删除）；
//  * 默认策略 evidence_only 惰性初始化（首读时建行+initial revision）；
//  * consent 撤销=UPDATE revoked_at（不删行）；同一 (tenant,provider,version) 唯一。
//  * Provider registry 只读注册面（DeepSeek 初始 custom_acknowledged/restricted_experiment，
//    不登记 zero_retention=true / training_disabled=true——GAP-ANALYSIS 实证纪律）。

import { validateReviewPolicy, MODE_CONTRACT } from './review-arch.mjs';

const SECRET_SHAPE_RE = /api[_-]?key|secret|password|private[_-]?key|^token$|bearer/i;

export const PROVIDER_REGISTRY_SEED = [
  {
    provider_id: 'deepseek',
    display_name: 'DeepSeek API',
    endpoint_origin: 'api.deepseek.com',
    policy_status: 'custom_acknowledged',
    // 如实登记（GAP-ANALYSIS）：零留存不可核验/无 DPA/条款 as long as necessary
    retention_summary: 'unknown_no_zdr_option_documented',
    training_summary: 'unknown_may_train_no_explicit_commitment',
    region_summary: 'not_disclosed',
    policy_reference: 'https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html',
    state: 'restricted_experiment',
  },
];

export function createReviewPolicyStore({ pool }) {
  const q = (text, params) => pool.query(text, params);

  async function ensureSeedProviders() {
    for (const p of PROVIDER_REGISTRY_SEED) {
      await q(
        `INSERT INTO mu.provider_registry (provider_id, display_name, endpoint_origin, policy_status,
           retention_summary, training_summary, region_summary, policy_reference, state)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
         ON CONFLICT (provider_id) DO NOTHING`,
        [p.provider_id, p.display_name, p.endpoint_origin, p.policy_status,
          p.retention_summary, p.training_summary, p.region_summary, p.policy_reference, p.state]);
    }
  }

  /** 读取策略（无行时惰性建默认 evidence_only+initial revision）。 */
  async function getPolicy(tenantId) {
    await ensureSeedProviders();
    let r = await q(`SELECT * FROM mu.review_policy WHERE tenant_id=$1`, [tenantId]);
    if (r.rows.length) return r.rows[0];
    const ins = await q(
      `INSERT INTO mu.review_policy (tenant_id, review_mode, policy_version)
       VALUES ($1,'evidence_only',1)
       ON CONFLICT (tenant_id) DO NOTHING
       RETURNING *`, [tenantId]);
    if (ins.rows.length) {
      await q(
        `INSERT INTO mu.review_policy_revision (tenant_id, policy_version, review_mode,
           code_egress_allowed, record_kind)
         VALUES ($1,1,'evidence_only',false,'initial')`, [tenantId]);
      return ins.rows[0];
    }
    r = await q(`SELECT * FROM mu.review_policy WHERE tenant_id=$1`, [tenantId]);
    return r.rows[0];
  }

  /**
   * CAS 更新（乐观锁）：expected_version 不匹配 → {ok:false, code:'version_conflict', current}。
   * 单事务：UPDATE 主行 + INSERT revision；域校验先行；secret 形状字段拒绝。
   */
  async function updatePolicy(tenantId, actorId, patch, { expectedVersion }) {
    const cur = await getPolicy(tenantId);
    const mode = patch.review_mode ?? cur.review_mode;
    if (!MODE_CONTRACT[mode]) return { ok: false, code: 'invalid_review_mode' };
    const merged = {
      review_mode: mode,
      provider_id: patch.provider_id ?? null,
      model_id: patch.model_id ?? null,
      provider_policy_status: patch.provider_policy_status ?? null,
      code_egress_allowed: MODE_CONTRACT[mode].code_egress_allowed,
      consent_version: patch.consent_version ?? null,
      context_budget: patch.context_budget ?? cur.context_budget ?? {},
      retention_ack: patch.retention_ack ?? false,
    };
    // secret 形状字段拒绝（零凭据纪律——即使客户端多传也拒）
    for (const k of Object.keys(patch ?? {})) {
      if (SECRET_SHAPE_RE.test(k)) return { ok: false, code: 'secret_field_forbidden' };
    }
    // external_api 前置：registry 是 provider_policy_status 唯一权威（patch 传入的忽略）。
    // 非 external 模式 status 置空（防携带陈旧语义）。
    if (merged.review_mode === 'external_api') {
      const prov = await q(
        `SELECT policy_status FROM mu.provider_registry WHERE provider_id=$1`, [merged.provider_id]);
      if (!prov.rows.length) return { ok: false, code: 'provider_not_registered' };
      if (prov.rows[0].policy_status === 'blocked') return { ok: false, code: 'provider_blocked' };
      merged.provider_policy_status = prov.rows[0].policy_status;
      const consent = await q(
        `SELECT 1 FROM mu.provider_consent
          WHERE tenant_id=$1 AND provider_id=$2 AND consent_version=$3
            AND revoked_at IS NULL AND code_egress_allowed=true`,
        [tenantId, merged.provider_id, merged.consent_version]);
      if (!consent.rows.length) return { ok: false, code: 'consent_version_required' };
    } else {
      merged.provider_policy_status = null;
    }
    const v = validateReviewPolicy(merged);
    if (!v.ok) return { ok: false, code: v.code };

    // 兼容 {query} 简化池（无 connect）：经 q 串行事务；CAS 由 WHERE policy_version 原子保证
    try {
      await q('BEGIN');
      const nextVersion = Number(cur.policy_version) + 1;
      const upd = await q(
        `UPDATE mu.review_policy SET
           review_mode=$2, provider_id=$3, model_id=$4, provider_policy_status=$5,
           code_egress_allowed=$6, consent_version=$7, context_budget=$8::jsonb,
           retention_ack=$9, enabled_at=now(), enabled_by=$10,
           policy_version=$11, updated_at=now()
         WHERE tenant_id=$1 AND policy_version=$12
         RETURNING *`,
        [tenantId, merged.review_mode, merged.provider_id, merged.model_id,
          merged.provider_policy_status, merged.code_egress_allowed, merged.consent_version,
          JSON.stringify(merged.context_budget), merged.retention_ack, actorId,
          nextVersion, Number(expectedVersion)]);
      if (!upd.rows.length) {
        await q('ROLLBACK');
        return { ok: false, code: 'version_conflict', current: await getPolicy(tenantId) };
      }
      await q(
        `INSERT INTO mu.review_policy_revision (tenant_id, policy_version, review_mode,
           provider_id, model_id, provider_policy_status, code_egress_allowed, consent_version,
           context_budget, retention_ack, enabled_by, enabled_at, record_kind)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,now(),'update')`,
        [tenantId, nextVersion, merged.review_mode, merged.provider_id, merged.model_id,
          merged.provider_policy_status, merged.code_egress_allowed, merged.consent_version,
          JSON.stringify(merged.context_budget), merged.retention_ack, actorId]);
      await q('COMMIT');
      return { ok: true, policy: upd.rows[0] };
    } catch (e) {
      await q('ROLLBACK').catch(() => {});
      return { ok: false, code: 'policy_update_failed', detail: String(e?.message ?? e).slice(0, 80) };
    }
  }

  async function listPolicyHistory(tenantId, limit = 50) {
    const r = await q(
      `SELECT * FROM mu.review_policy_revision WHERE tenant_id=$1
        ORDER BY policy_version DESC, revision_id DESC LIMIT $2`, [tenantId, limit]);
    return r.rows;
  }

  async function listProviders() {
    await ensureSeedProviders();
    const r = await q(
      `SELECT provider_id, display_name, endpoint_origin, policy_status,
              retention_summary, training_summary, region_summary, policy_reference,
              reviewed_at, reviewed_by, state
         FROM mu.provider_registry ORDER BY provider_id`);
    return r.rows;
  }

  /** 接受 consent（幂等重接受=恢复：撤销后再接受同版本 → 恢复该版本有效）。 */
  async function acceptConsent(tenantId, actorId, { providerId, consentVersion, acknowledgementDigest, policyVersion }) {
    if (!providerId || !consentVersion || !acknowledgementDigest) {
      return { ok: false, code: 'consent_payload_invalid' };
    }
    await q(
      `INSERT INTO mu.provider_consent (tenant_id, provider_id, consent_version, policy_version,
         accepted_by, acknowledgement_digest, code_egress_allowed)
       VALUES ($1,$2,$3,$4,$5,$6,true)
       ON CONFLICT (tenant_id, provider_id, consent_version)
       DO UPDATE SET revoked_at=NULL, revoked_by=NULL, accepted_at=now(), accepted_by=$5,
         acknowledgement_digest=$6, policy_version=$4`,
      [tenantId, providerId, consentVersion, Number(policyVersion) || 1, actorId, acknowledgementDigest]);
    return { ok: true };
  }

  /** 撤销 consent（不删行；未来 run 的 external_api 即时失效）。 */
  async function revokeConsent(tenantId, actorId, providerId) {
    await q(
      `UPDATE mu.provider_consent SET revoked_at=now(), revoked_by=$3
        WHERE tenant_id=$1 AND provider_id=$2 AND revoked_at IS NULL`,
      [tenantId, providerId, actorId]);
    return { ok: true };
  }

  /** evaluateEgressAuthorization 的实时态装配（供 API 与未来 adapter 共用）。 */
  async function getEgressCurrentState(tenantId, providerId) {
    const [policy, provider, consent] = await Promise.all([
      q(`SELECT * FROM mu.review_policy WHERE tenant_id=$1`, [tenantId]),
      q(`SELECT policy_status FROM mu.provider_registry WHERE provider_id=$1`, [providerId ?? '']),
      // 查最新行（含已撤销）——evaluateEgressAuthorization 需区分"从未同意"（MISSING）
      // 与"曾同意已撤销"（REVOKED）两种稳定 reason
      q(`SELECT revoked_at, code_egress_allowed, consent_version FROM mu.provider_consent
          WHERE tenant_id=$1 AND provider_id=$2
          ORDER BY (revoked_at IS NULL) DESC, accepted_at DESC LIMIT 1`, [tenantId, providerId ?? '']),
    ]);
    return {
      policy: policy.rows[0] ?? null,
      provider: provider.rows[0] ?? null,
      consent: consent.rows[0] ?? null,
      // kill switch：环境级（PR A 只读 env；租户级 UI 开关留 PR F）
      tenantDisabled: false,
      globalDisabled: process.env.MU_REVIEW_EGRESS_KILL_SWITCH === '1',
    };
  }

  return { getPolicy, updatePolicy, listPolicyHistory, listProviders,
    acceptConsent, revokeConsent, getEgressCurrentState, ensureSeedProviders };
}
