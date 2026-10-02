// console/backend/lib/multiuser/review-arch-enums.mjs — ADR-002 PR A：稳定机器枚举。
//
// 纪律：这里只有机器枚举与稳定 reason code——前端显示文案一律不进入（PR F 职责）。
// 未知值一律 fail-closed（解析函数返回 rejected 而非猜测）。

export const REVIEW_MODES = Object.freeze(['evidence_only', 'external_api', 'local']);
export const REVIEW_SCOPES = Object.freeze(['evidence_only', 'full_code']);
export const EXECUTION_MODES = Object.freeze(['none', 'external_api', 'local']);
export const PROVIDER_POLICY_STATUSES = Object.freeze(['verified', 'custom_acknowledged', 'blocked']);
export const REVIEW_VERDICTS = Object.freeze(['not_run', 'no_blocking_findings', 'changes_requested', 'inconclusive']);
export const VERIFICATION_VERDICTS = Object.freeze(['not_run', 'passed', 'failed', 'inconclusive']);
export const TESTS_STATUSES = Object.freeze(['not_run', 'passed', 'failed', 'unavailable']);
export const MERGE_ELIGIBILITIES = Object.freeze(['unknown', 'eligible', 'ineligible']);

// 模式 → scope/execution/egress 推导（单一权威表；DB CHECK 与服务校验共用语义）
export const MODE_CONTRACT = Object.freeze({
  evidence_only: { review_scope: 'evidence_only', execution_mode: 'none', code_egress_allowed: false },
  external_api: { review_scope: 'full_code', execution_mode: 'external_api', code_egress_allowed: true },
  local: { review_scope: 'full_code', execution_mode: 'local', code_egress_allowed: false },
});

export const ARCH_VERSION = 'v2';

// egress authorization 稳定 reason codes（六.节）
export const EGRESS_DENY_REASONS = Object.freeze([
  'EGRESS_CONSENT_REVOKED',
  'EGRESS_PROVIDER_BLOCKED',
  'EGRESS_TENANT_DISABLED',
  'EGRESS_GLOBAL_DISABLED',
  'EGRESS_SNAPSHOT_INVALID',
  'EGRESS_POLICY_MISMATCH',
  'EGRESS_POLICY_MISSING',
  'EGRESS_CONSENT_MISSING',
  'EGRESS_MODE_NOT_EXTERNAL',
]);

function isOneOf(list, v) {
  return typeof v === 'string' && list.includes(v);
}

/** 枚举解析（fail-closed）：未知/缺失 → {ok:false, code}。 */
export function parseReviewEnums(raw = {}) {
  const out = {};
  if (raw.review_mode !== undefined) {
    if (!isOneOf(REVIEW_MODES, raw.review_mode)) return { ok: false, code: 'invalid_review_mode' };
    out.review_mode = raw.review_mode;
  }
  if (raw.provider_policy_status !== undefined) {
    if (!isOneOf(PROVIDER_POLICY_STATUSES, raw.provider_policy_status)) {
      return { ok: false, code: 'invalid_provider_policy_status' };
    }
    out.provider_policy_status = raw.provider_policy_status;
  }
  for (const [k, list] of [
    ['review_verdict', REVIEW_VERDICTS], ['verification_verdict', VERIFICATION_VERDICTS],
    ['tests_status', TESTS_STATUSES], ['merge_eligibility', MERGE_ELIGIBILITIES],
  ]) {
    if (raw[k] !== undefined) {
      if (!isOneOf(list, raw[k])) return { ok: false, code: `invalid_${k}` };
      out[k] = raw[k];
    }
  }
  return { ok: true, values: out };
}

/**
 * 策略域校验（三/四/五节不变量的服务层权威；DB CHECK 只覆盖单行可表达部分）。
 * 输入：candidate={review_mode, provider_id?, model_id?, provider_policy_status?,
 *   code_egress_allowed, consent_version?, retention_ack?}
 * 返回 {ok:true} 或 {ok:false, code}（稳定 reason）。
 */
export function validateReviewPolicy(candidate) {
  if (!candidate || typeof candidate !== 'object') return { ok: false, code: 'policy_invalid_shape' };
  const mode = candidate.review_mode;
  if (!isOneOf(REVIEW_MODES, mode)) return { ok: false, code: 'invalid_review_mode' };
  const contract = MODE_CONTRACT[mode];

  if (candidate.code_egress_allowed !== contract.code_egress_allowed) {
    return { ok: false, code: 'egress_mode_mismatch' }; // 不变量 1/3：evidence/local 必须 false，external 必须 true
  }
  if (mode === 'external_api') {
    if (!candidate.provider_id) return { ok: false, code: 'provider_id_required' };
    if (String(candidate.provider_id).length > 120) return { ok: false, code: 'provider_id_invalid' };
    if (!candidate.model_id) return { ok: false, code: 'model_id_required' };
    if (String(candidate.model_id).length > 200) return { ok: false, code: 'model_id_invalid' };
    // 不变量 4：blocked 不能用于 external_api；verified/custom_acknowledged 均可（后者须 consent+retention_ack）
    if (candidate.provider_policy_status === 'blocked') return { ok: false, code: 'provider_blocked' };
    if (!isOneOf(PROVIDER_POLICY_STATUSES, candidate.provider_policy_status)) {
      return { ok: false, code: 'invalid_provider_policy_status' };
    }
    if (!candidate.consent_version) return { ok: false, code: 'consent_version_required' };
    if (candidate.provider_policy_status === 'custom_acknowledged' && candidate.retention_ack !== true) {
      return { ok: false, code: 'retention_ack_required_for_custom' };
    }
  } else {
    // evidence_only / local：provider 字段应清空（防携带陈旧 provider 语义）
    if (candidate.provider_id || candidate.consent_version) {
      return { ok: false, code: 'provider_fields_only_for_external' };
    }
  }
  return { ok: true };
}

/**
 * 实时出站安全否决层（六.节）：snapshot 允许出站也必须过实时条件。
 * 纯函数——后续所有 Provider adapter 统一调用，禁止各调用点自行判断。
 * 输入：
 *   snapshot   = run 冻结的 review_policy_snapshot（含 snapshot_digest）
 *   current    = { policy: 当前 mu.review_policy 行, consent: {revoked_at, code_egress_allowed}|null,
 *                  provider: {policy_status}|null, tenantDisabled: bool, globalDisabled: bool }
 * 返回 {authorized: true} 或 {authorized: false, reason}
 */
export function evaluateEgressAuthorization(snapshot, current) {
  if (!snapshot || typeof snapshot !== 'object' || !snapshot.snapshot_digest) {
    return { authorized: false, reason: 'EGRESS_SNAPSHOT_INVALID' };
  }
  if (snapshot.review_mode !== 'external_api' || snapshot.code_egress !== true) {
    return { authorized: false, reason: 'EGRESS_MODE_NOT_EXTERNAL' };
  }
  if (current.globalDisabled) return { authorized: false, reason: 'EGRESS_GLOBAL_DISABLED' };
  if (current.tenantDisabled) return { authorized: false, reason: 'EGRESS_TENANT_DISABLED' };
  // tenant/provider 与 snapshot 一致（防串租户/串 Provider）——先于 null 检查：
  // 跨租户场景（policy 属别的租户或无该 provider 配置）报 MISMATCH 而非 MISSING
  if (current.policy && (String(current.policy.tenant_id) !== String(snapshot.tenant_id)
    || String(current.policy.provider_id ?? '') !== String(snapshot.provider_id ?? ''))) {
    return { authorized: false, reason: 'EGRESS_POLICY_MISMATCH' };
  }
  if (!current.policy || !current.provider || !current.consent) {
    return { authorized: false, reason: 'EGRESS_POLICY_MISSING' };
  }
  if (current.provider.policy_status === 'blocked') return { authorized: false, reason: 'EGRESS_PROVIDER_BLOCKED' };
  if (current.consent.revoked_at) return { authorized: false, reason: 'EGRESS_CONSENT_REVOKED' };
  if (current.consent.code_egress_allowed !== true
    || String(current.consent.consent_version) !== String(snapshot.consent_version)) {
    return { authorized: false, reason: 'EGRESS_CONSENT_MISSING' };
  }
  return { authorized: true };
  // 注：store.getEgressCurrentState 返回最新 consent 行（含已撤销）——
  // null=从未同意（EGRESS_POLICY_MISSING 语义）；revoked_at 非空=已撤销（本分支）。
}

/**
 * 构建不可变 run 快照（六.节）。digest=内容哈希（sha256 截断十六进制）——
 * 不含凭据；context_budget 原样冻结。
 */
export function buildPolicySnapshot({ tenantId, policy, arch = ARCH_VERSION }) {
  const contract = MODE_CONTRACT[policy.review_mode];
  const body = {
    architecture_version: arch,
    tenant_id: String(tenantId),
    review_mode: policy.review_mode,
    review_scope: contract.review_scope,
    execution_mode: contract.execution_mode,
    provider_id: policy.provider_id ?? null,
    model_id: policy.model_id ?? null,
    provider_policy_status: policy.provider_policy_status ?? null,
    code_egress: contract.code_egress_allowed,
    consent_version: policy.consent_version ?? null,
    policy_version: Number(policy.policy_version),
    context_budget: policy.context_budget ?? null,
  };
  return { ...body, snapshot_digest: digestOfSnapshot(body) };
}

import crypto from 'node:crypto';
export function digestOfSnapshot(body) {
  const stable = JSON.stringify(body, Object.keys(body).sort());
  return crypto.createHash('sha256').update(stable).digest('hex').slice(0, 32);
}
