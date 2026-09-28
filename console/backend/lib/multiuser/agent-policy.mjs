// console/backend/lib/multiuser/agent-policy.mjs — Wave 3.2：Agent 运行策略控制面。
//
// 配置层级（任务书裁决）：
//  * 部署级（仅服务器 env/Secret Manager）：API key、base URL、provider 可用性
//  * 平台级（PlatformAdmin 可管理，本模块）：mode/enabled/model/timeout/tokens
//  * 禁止：用户级 key、浏览器直连模型、租户任意 endpoint——本模块无这些字段
//
// 安全契约：
//  * 单行表（id=1）+ DB CHECK（模式/provider 枚举、模型字符形状、数值区间）；
//  * 更新=乐观并发 CAS（WHERE policy_version=expected → 409 语义）；
//  * 服务端白名单二次校验（模型必须 ∈ env MU_LLM_ALLOWED_MODELS，默认
//    deepseek-flash/deepseek-chat；provider 仅 openai_compatible）；
//  * 拒绝任何凭据形状字段进入本表/API/审计（api_key/token/secret/password/private）；
//  * 策略只影响新 review run：run 创建时冻结 policy_version+llm_mode 快照列；
//    LLM 阶段使用创建时解析的快照，运行中不受更新影响；
//  * llm_assist 启用但部署 env 缺失/非法 → fail-closed 回落 deterministic，
//    稳定 reason（LLM_POLICY_ENV_MISMATCH / LLM_POLICY_MODEL_NOT_ALLOWED），
//    绝不误报 LLM 已执行。
import { resolveLlmProvider } from './agents/llm.mjs';

export const DEFAULT_ALLOWED_MODELS = ['deepseek-flash', 'deepseek-chat'];
const MODEL_SHAPE = /^[a-z0-9][a-z0-9._/-]{0,63}$/;
const SECRET_FIELD_RE = /api[_-]?key|secret|password|private[_-]?key|^token$/i; // token 仅整词命中——max_output_tokens 是合法字段
const LIMITS = { timeoutMin: 1000, timeoutMax: 120_000, tokensMin: 64, tokensMax: 4096 };

/** env 白名单（部署级模型允许清单）。 */
export function allowedModels(env = process.env) {
  const raw = String(env.MU_LLM_ALLOWED_MODELS ?? '').trim();
  if (!raw) return [...DEFAULT_ALLOWED_MODELS];
  return raw.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean).slice(0, 20);
}

/** 部署级安全状态（只读、脱敏——仅布尔/host 摘要，永不含完整 URL/key）。 */
export function deployStatus(env = process.env) {
  const p = resolveLlmProvider(env);
  let hostSummary = null;
  if (p.kind === 'openai_compatible') {
    try { hostSummary = new URL(p.baseUrl).host; } catch { hostSummary = null; }
  }
  return { provider_configured: p.kind === 'openai_compatible', provider_host_summary: hostSummary,
    allowed_models: allowedModels(env) };
}

/** 读取当前策略（表未建/空时返回默认语义行——fail-closed 默认 disabled）。 */
export async function getAgentPolicy(pool) {
  const r = await pool.query(`SELECT * FROM mu.agent_policy WHERE id=1`);
  if (r.rows.length) return r.rows[0];
  return { id: 1, mode: 'deterministic_only', provider: 'openai_compatible',
    model: 'deepseek-flash', timeout_ms: 30000, max_output_tokens: 1024,
    enabled: false, policy_version: 1, updated_by: null,
    created_at: null, updated_at: null };
}

/** 更新校验（服务端白名单；返回 {ok} 或 {ok:false, code}）。 */
export function validatePolicyPatch(patch, env = process.env) {
  const bad = Object.keys(patch ?? {}).find((k) => SECRET_FIELD_RE.test(k));
  if (bad) return { ok: false, code: 'secret_field_forbidden' };
  const allow = new Set(['mode', 'provider', 'model', 'timeout_ms', 'max_output_tokens', 'enabled']);
  for (const k of Object.keys(patch ?? {})) if (!allow.has(k)) return { ok: false, code: 'field_not_allowed' };
  if (patch.mode !== undefined && !['deterministic_only', 'llm_assist'].includes(patch.mode)) {
    return { ok: false, code: 'mode_invalid' };
  }
  if (patch.provider !== undefined && patch.provider !== 'openai_compatible') {
    return { ok: false, code: 'provider_not_allowed' };
  }
  if (patch.model !== undefined) {
    if (!MODEL_SHAPE.test(String(patch.model))) return { ok: false, code: 'model_shape_invalid' };
    if (!allowedModels(env).includes(String(patch.model))) return { ok: false, code: 'model_not_allowed' };
  }
  if (patch.timeout_ms !== undefined
    && (Number(patch.timeout_ms) < LIMITS.timeoutMin || Number(patch.timeout_ms) > LIMITS.timeoutMax)) {
    return { ok: false, code: 'timeout_out_of_range' };
  }
  if (patch.max_output_tokens !== undefined
    && (Number(patch.max_output_tokens) < LIMITS.tokensMin || Number(patch.max_output_tokens) > LIMITS.tokensMax)) {
    return { ok: false, code: 'tokens_out_of_range' };
  }
  if (patch.enabled !== undefined && typeof patch.enabled !== 'boolean') {
    return { ok: false, code: 'enabled_invalid' };
  }
  return { ok: true };
}

/** CAS 更新：expectedVersion 不匹配 → {ok:false, code:'version_conflict', current}。 */
export async function updateAgentPolicy(pool, { expectedVersion, patch, actorId }) {
  const fields = [];
  const params = [];
  const set = (col, val) => { params.push(val); fields.push(`${col}=$${params.length}`); };
  if (patch.mode !== undefined) set('mode', patch.mode);
  if (patch.provider !== undefined) set('provider', patch.provider);
  if (patch.model !== undefined) set('model', patch.model);
  if (patch.timeout_ms !== undefined) set('timeout_ms', Number(patch.timeout_ms));
  if (patch.max_output_tokens !== undefined) set('max_output_tokens', Number(patch.max_output_tokens));
  if (patch.enabled !== undefined) set('enabled', patch.enabled);
  if (!fields.length) return { ok: false, code: 'empty_patch' };
  set('updated_by', actorId);
  params.push(Number(expectedVersion));
  const r = await pool.query(
    `UPDATE mu.agent_policy SET ${fields.join(', ')}, policy_version = policy_version + 1, updated_at = now()
      WHERE id = 1 AND policy_version = $${params.length}
      RETURNING *`, params);
  if (!r.rows.length) {
    const cur = await getAgentPolicy(pool);
    return { ok: false, code: 'version_conflict', current: cur };
  }
  return { ok: true, policy: r.rows[0] };
}

/**
 * 有效策略合成（新 run 创建时调用一次，之后冻结）：
 * policy.enabled && mode=llm_assist ∧ 部署 env 合法 ∧ model ∈ 白名单
 *   → {kind:'openai_compatible', baseUrl/model/timeout/maxTokens（策略覆盖 env 数值）, apiKeyRef:'env'}
 * 否则 → {kind:'disabled', reason}（fail-closed；policy 想开但开不成时 reason 区分）。
 * 返回值永不含 key 本体（调用方从 env 现取）。
 */
export function resolveEffectiveLlmPolicy(policy, env = process.env) {
  const dep = resolveLlmProvider(env);
  const wantsLlm = Boolean(policy?.enabled) && policy?.mode === 'llm_assist';
  if (!wantsLlm) return { kind: 'disabled', reason: policy?.enabled ? 'llm_mode_off' : 'policy_disabled' };
  if (dep.kind === 'deterministic_mock') {
    // 测试部署显式声明（env 控制）——attempt provider 如实标记 deterministic_mock
    return { kind: 'deterministic_mock' };
  }
  if (dep.kind !== 'openai_compatible') {
    return { kind: 'disabled', reason: 'LLM_POLICY_ENV_MISMATCH' };
  }
  if (!allowedModels(env).includes(String(policy.model))) {
    return { kind: 'disabled', reason: 'LLM_POLICY_MODEL_NOT_ALLOWED' };
  }
  return { kind: 'openai_compatible', baseUrl: dep.baseUrl,
    model: String(policy.model),
    timeout: Number(policy.timeout_ms) || dep.timeout,
    maxTokens: Number(policy.max_output_tokens) || dep.maxTokens,
    apiKeyRef: 'env' };
}

/** runtime_state 展示态（脱敏，供 GET 与前端）。优先级：策略想开而开不成 > env 态。 */
export function runtimeState(policy, env = process.env) {
  const wantsLlm = Boolean(policy?.enabled) && policy?.mode === 'llm_assist';
  if (wantsLlm) {
    const eff = resolveEffectiveLlmPolicy(policy, env);
    return (eff.kind === 'openai_compatible' || eff.kind === 'deterministic_mock')
      ? 'llm_assist_active' : 'policy_enabled_but_env_invalid';
  }
  return deployStatus(env).provider_configured
    ? 'configured_disabled_or_det_only' : 'env_not_configured';
}
