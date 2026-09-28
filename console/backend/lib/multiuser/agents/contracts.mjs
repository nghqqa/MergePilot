// console/backend/lib/multiuser/agents/contracts.mjs — Wave 3 PR-C：Agent 角色契约。
// 四角色（Leader/Reviewer/Fixer/Verifier）的消息绑定与结果 schema——所有 Agent 消息
// 必须携带完整绑定五元组；跨 run/tenant/repo/pr/head 的消息一律拒绝。
export const AGENT_ROLES = Object.freeze(['leader', 'reviewer', 'fixer', 'verifier']);

/** 绑定五元组校验：消息与期望上下文必须逐字段相等（防跨 run/跨 head 投递）。 */
export function validateAgentMessage(msg, expected) {
  if (!msg || typeof msg !== 'object') return { ok: false, reason: 'msg_invalid' };
  const fields = ['run_id', 'tenant_id', 'repo_id', 'pr_id', 'head_sha', 'agent_role', 'attempt'];
  for (const f of fields) {
    if (!(f in msg)) return { ok: false, reason: `missing_${f}` };
    if (String(msg[f]) !== String(expected[f])) return { ok: false, reason: `binding_mismatch_${f}` };
  }
  if (!AGENT_ROLES.includes(msg.agent_role)) return { ok: false, reason: 'role_invalid' };
  if (!Number.isInteger(Number(msg.attempt)) || Number(msg.attempt) < 1) {
    return { ok: false, reason: 'attempt_invalid' };
  }
  return { ok: true };
}

// 结果 schema（allowlist：多余键剔除；必填键缺失拒绝）
const SEVERITIES = ['P0', 'P1', 'P2', 'P3'];
const SCHEMAS = Object.freeze({
  reviewer: {
    required: ['findings'],
    shape: {
      findings: (v) => Array.isArray(v) && v.every((f) => f && typeof f === 'object'
        && typeof f.rule_id === 'string' && typeof f.path === 'string'
        && SEVERITIES.includes(f.severity)),
      citations: (v) => v === undefined || (Array.isArray(v)
        && v.every((c) => typeof c === 'string' && c.length <= 300)),
    },
  },
  fixer: {
    required: ['status', 'patch_digest'],
    shape: {
      status: (v) => ['PLANNED', 'DRY_RUN', 'FAILED', 'SKIPPED'].includes(v),
      patch_digest: (v) => v === null || /^[0-9a-f]{64}$/.test(String(v)),
    },
  },
  verifier: {
    required: ['verdict'],
    shape: {
      verdict: (v) => ['PASS', 'FAIL', 'BLOCKED'].includes(v),
      citations: (v) => v === undefined || (Array.isArray(v) && v.every((c) => typeof c === 'string')),
    },
  },
});

const MAX_RESULT_BYTES = 64 * 1024;

/** Agent 结果校验（大小上限 + schema allowlist；LLM 输出必经此处）。 */
export function validateAgentResult(role, result) {
  if (!SCHEMAS[role]) return { ok: false, reason: 'role_unknown' };
  try {
    if (Buffer.byteLength(JSON.stringify(result ?? {})) > MAX_RESULT_BYTES) {
      return { ok: false, reason: 'result_oversize' };
    }
  } catch { return { ok: false, reason: 'result_unserializable' }; }
  const schema = SCHEMAS[role];
  for (const k of schema.required) {
    if (!(k in (result ?? {})) ) return { ok: false, reason: `missing_${k}` };
  }
  for (const [k, check] of Object.entries(schema.shape)) {
    if (result?.[k] === undefined) continue;
    if (!check(result[k])) return { ok: false, reason: `bad_${k}` };
  }
  return { ok: true };
}

/** LLM 结论的 citation 门槛：无 citation 的 LLM 结论不得标记 verified。 */
export function llmVerifiedAllowed(result) {
  const cits = result?.citations ?? [];
  return Array.isArray(cits) && cits.length > 0
    && cits.every((c) => typeof c === 'string' && /^[\w./:#-]{1,300}$/.test(c));
}
