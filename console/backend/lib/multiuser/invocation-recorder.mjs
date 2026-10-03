// console/backend/lib/multiuser/invocation-recorder.mjs — C 波 C1：Skill/RAG 调用留痕唯一写入口。
//
// 纪律（任务书 C1 + 仓库审计惯例；表见 schema.mjs migration 19）：
//  * 唯一服务端写入口——Agent/管线不得自行写 mu.skill_invocation_event /
//    mu.rag_retrieval_event（本模块是这两张表的全部 INSERT/UPDATE 面）；
//  * 敏感数据硬边界：只接受 digest（hex）/计数/脱敏错误码。运行时守卫
//    guardInvocationMetadata 递归扫描全部字符串参数：超长（>512）或命中
//    secret/DSN/私钥/Bearer 形状即整体拒绝（sensitive_param_rejected），拒绝
//    结果只含字段名，绝不回显值——prompt/response/query 原文/代码正文在结构上
//    无法经本模块入库或入日志（本模块零 console 输出）；
//  * 活跃门（gate）：mu.skill.state='active' 且版本匹配 current_version——不活跃
//    拒绝执行并记 FAILED 事件（error_code='skill_inactive'）；传入版本与现行版本
//    不符 → 'version_mismatch'。版本绑定在门处完成（调用方不传版本 → 绑定
//    current_version）。未注册技能（无 mu.skill 行）→ 如实放行且 skill_version
//    置空（不虚构治理结论——留痕不等于治理，治理以 mu.skill 注册为准）；
//  * fail-closed 门：tenant 非 active → 'tenant_disabled'（全部 kind）；
//    external 类 kind（agentteams_round/skill_mcp）且 run.review_mode='external_api'
//    时：provider 被 block → 'provider_blocked'；无未撤销且允许出站的 consent →
//    'consent_revoked'。门拒绝=拒绝执行+记 FAILED 事件（绝不放行）；
//  * evidence-only（run.review_mode='evidence_only'）：finish 不允许 SUCCEEDED
//    （rejected: 'evidence_only_success_rejected'）——调用方应记 FAILED +
//    error_code='evidence_only_no_external_calls' 或直接不记成功。
//    （选择说明：门只作用于 run 域事件——mu.review_policy 的惰性默认即
//    evidence_only，按租户策略拦截会误杀全部默认租户，故以 run 行显式证据为准。）
//  * 幂等：UNIQUE (tenant_id, idempotency_key)——同键返回既有事件（idempotent:true）；
//    重试=新事件新键（追加不覆盖）。已完成事件 UPDATE/DELETE 由 DB 触发器封印，
//    本模块 finish 只做 RUNNING→终态一次流转（WHERE status='RUNNING' CAS）；
//  * run/attempt 归属：recorder 服务端校验 run 必须同 tenant/repo/pr，attempt 必须
//    属于该 run——不符拒绝写入（DB 复合 FK 兜底，双层防御）；
//  * 本模块任何函数不抛业务异常（返回 {ok:false, code}）；pool 错误同样收敛为
//    {ok:false, code:'recorder_db_error'}——留痕故障不得放大为管线故障。
import crypto from 'node:crypto';

export const INVOCATION_STATUSES = Object.freeze([
  'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED', 'INTERRUPTED']);
export const TERMINAL_INVOCATION_STATUSES = Object.freeze(
  new Set(['SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED', 'INTERRUPTED']));
export const INVOCATION_KINDS = Object.freeze([
  'verifier_tool', 'agentteams_round', 'skill_mcp', 'rag_query', 'other']);
export const AGENT_ROLES = Object.freeze(
  ['leader', 'reviewer', 'fixer', 'verifier', 'system']);
// external 类 kind：受 provider/consent fail-closed 门约束（本地数据面不受）
const EXTERNAL_KINDS = Object.freeze(new Set(['agentteams_round', 'skill_mcp']));

export const sha256Hex = (v) =>
  crypto.createHash('sha256').update(String(v ?? '')).digest('hex');

// ── 敏感形状正则（DSN/私钥/token/Bearer——出现即整体拒绝，绝不入库） ──
const SENSITIVE_SHAPE_RE = new RegExp(
  '(postgres(?:ql)?://[^\\s:/]+:[^\\s@]+@'          // DSN（含凭据形状）
  + '|-----BEGIN [A-Z ]*PRIVATE KEY-----'            // 私钥
  + '|sk-[A-Za-z0-9]{12,}'                           // OpenAI 形状 key
  + '|ghp_[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{16,}'     // GitHub PAT
  + '|github_pat_[A-Za-z0-9_]{20,}'
  + '|AKIA[0-9A-Z]{12,}'                             // AWS AKIA
  + '|Bearer [A-Za-z0-9._~+/=-]{16,})',              // Authorization 形状
  'i');
const MAX_STRING_LEN = 512;

const HEX_RE = (min, max) => new RegExp(`^[0-9a-f]{${min},${max}}$`);
const isHexDigest = (v, min = 8, max = 64) =>
  typeof v === 'string' && HEX_RE(min, max).test(v);

/**
 * 运行时元数据守卫：递归（深度≤2）收集字符串值，命中即拒绝。
 * 返回 {ok:true} 或 {ok:false, code:'sensitive_param_rejected', field}——field 只含
 * 字段路径，值绝不回显（守卫自身的输出也是脱敏边界的一部分）。
 */
export function guardInvocationMetadata(params, { maxLen = MAX_STRING_LEN } = {}) {
  const visit = (prefix, v, depth) => {
    if (v == null) return null;
    if (typeof v === 'string') {
      if (v.length > maxLen) return prefix || '(root)';
      if (SENSITIVE_SHAPE_RE.test(v)) return prefix || '(root)';
      return null;
    }
    if (depth <= 0) return null;
    if (Array.isArray(v)) {
      for (let i = 0; i < Math.min(v.length, 128); i++) {
        const hit = visit(`${prefix}[${i}]`, v[i], depth - 1);
        if (hit) return hit;
      }
      return null;
    }
    if (typeof v === 'object') {
      for (const k of Object.keys(v)) {
        const hit = visit(prefix ? `${prefix}.${k}` : k, v[k], depth - 1);
        if (hit) return hit;
      }
    }
    return null;
  };
  const field = visit('', params, 2);
  if (field) return { ok: false, code: 'sensitive_param_rejected', field };
  return { ok: true };
}

/** API/日志白名单投影（与 api.mjs 只读端点共用——idempotency_key 永不外发）。 */
export function projectSkillEvent(row) {
  if (!row) return null;
  return {
    event_id: row.event_id,
    agent_role: row.agent_role,
    skill_key: row.skill_key,
    skill_version: row.skill_version,
    invocation_kind: row.invocation_kind,
    status: row.status,
    started_at: row.started_at,
    completed_at: row.completed_at,
    latency_ms: row.latency_ms == null ? null : Number(row.latency_ms),
    input_digest: row.input_digest,
    output_digest: row.output_digest,
    error_code: row.error_code,
  };
}

export function projectRagEvent(row) {
  if (!row) return null;
  return {
    event_id: row.event_id,
    agent_role: row.agent_role,
    skill_key: row.skill_key,
    status: row.status,
    started_at: row.started_at,
    completed_at: row.completed_at,
    latency_ms: row.latency_ms == null ? null : Number(row.latency_ms),
    error_code: row.error_code,
    query_digest: row.query_digest,
    result_count: row.result_count == null ? null : Number(row.result_count),
    source_digest_list: row.source_digest_list,
  };
}

const latencySince = (startedAt) => {
  const ms = Math.round((Date.now() - new Date(startedAt).getTime()));
  return Number.isFinite(ms) && ms >= 0 ? ms : 0;
};

/** 事件行 → {ok, eventId, event}（统一白名单投影；永不携带 idempotency_key）。 */
const skillEventResult = (row, extra = {}) =>
  ({ ok: true, eventId: row.event_id, event: projectSkillEvent(row), ...extra });

// ── 归属校验（recorder 服务端前置；DB 复合 FK 兜底） ──
async function validateOwnership(q, { tenantId, repoId, prId = null, runId = null, attemptId = null }) {
  // repo 必须归属 tenant（rag 无 run 路径的显式锚定）
  const repo = await q(
    `SELECT 1 FROM mu.repository WHERE repo_id=$1 AND tenant_id=$2`, [repoId, tenantId]);
  if (!repo.rows.length) return { ok: false, code: 'repo_mismatch' };
  if (!runId) return { ok: true, run: null };
  const run = (await q(
    `SELECT run_id, tenant_id, repo_id, pr_id, review_mode, provider_id
       FROM mu.review_run WHERE run_id=$1`, [runId])).rows[0];
  if (!run) return { ok: false, code: 'run_not_found' };
  if (String(run.tenant_id) !== String(tenantId)
    || String(run.repo_id) !== String(repoId)
    || (prId != null && String(run.pr_id) !== String(prId))) {
    return { ok: false, code: 'run_mismatch' };
  }
  if (attemptId) {
    const att = (await q(
      `SELECT run_id, tenant_id FROM mu.agent_attempt WHERE attempt_id=$1`, [attemptId])).rows[0];
    if (!att) return { ok: false, code: 'attempt_not_found' };
    if (String(att.run_id) !== String(runId) || String(att.tenant_id) !== String(tenantId)) {
      return { ok: false, code: 'attempt_mismatch' };
    }
  }
  return { ok: true, run };
}

// ── fail-closed 门：tenant 停用（全部 kind）/ provider blocked / consent 撤销（external kind） ──
async function checkFailClosedGates(q, { tenantId, run, invocationKind }) {
  const t = (await q(`SELECT state FROM mu.tenant WHERE tenant_id=$1`, [tenantId])).rows[0];
  if (!t || t.state !== 'active') return 'tenant_disabled';
  if (EXTERNAL_KINDS.has(invocationKind) && run && String(run.review_mode ?? '') === 'external_api') {
    if (!run.provider_id) return 'provider_blocked';
    const p = (await q(
      `SELECT policy_status FROM mu.provider_registry WHERE provider_id=$1`,
      [run.provider_id])).rows[0];
    if (!p || p.policy_status === 'blocked') return 'provider_blocked';
    const c = await q(
      `SELECT 1 FROM mu.provider_consent
        WHERE tenant_id=$1 AND provider_id=$2 AND revoked_at IS NULL AND code_egress_allowed=true
        ORDER BY accepted_at DESC LIMIT 1`, [tenantId, run.provider_id]);
    if (!c.rows.length) return 'consent_revoked';
  }
  return null;
}

// ── 活跃门 + 版本绑定（未注册技能：如实放行、skill_version 置空） ──
async function checkSkillGate(q, { tenantId, skillKey, skillVersion }) {
  const s = (await q(
    `SELECT state, current_version FROM mu.skill WHERE tenant_id=$1 AND skill_key=$2`,
    [tenantId, skillKey])).rows[0];
  if (!s) return { kind: 'unregistered', boundVersion: null };
  if (s.state !== 'active') return { kind: 'inactive', boundVersion: null };
  if (skillVersion != null && String(skillVersion) !== String(s.current_version ?? '')) {
    return { kind: 'version_mismatch', boundVersion: null };
  }
  return { kind: 'ok', boundVersion: skillVersion != null ? String(skillVersion)
    : (s.current_version == null ? null : String(s.current_version)) };
}

/** 幂等取既有事件（同 (tenant, key) 返回既有行——webhook/retry 不重复写）。 */
async function findExisting(q, { tenantId, idempotencyKey }) {
  if (!tenantId || !idempotencyKey) return null;
  return (await q(
    `SELECT * FROM mu.skill_invocation_event WHERE tenant_id=$1 AND idempotency_key=$2`,
    [tenantId, idempotencyKey])).rows[0] ?? null;
}

/**
 * Skill 调用开始（唯一入口）。
 * 顺序：元数据守卫 → 幂等查重 → run/attempt 归属 → fail-closed 门 → 活跃门/版本绑定
 * → INSERT（RUNNING；门拒绝则直接 INSERT 终态 FAILED 并返回 ok:false——调用方必须
 * 中止执行）。返回：
 *  * {ok:true,  eventId, event, skillVersion, idempotent?} —— 已放行，可执行；
 *  * {ok:false, code, eventId?, event?}                    —— 拒绝执行（事件已记档）。
 */
export async function recordSkillInvocationStart(pool, { tenantId, repoId, prId, runId,
  attemptId = null, agentRole, skillKey, skillVersion = null, invocationKind,
  idempotencyKey, inputDigest = null }) {
  const q = (text, params) => pool.query(text, params);
  const guarded = guardInvocationMetadata({ tenantId, repoId, prId, runId, attemptId,
    agentRole, skillKey, skillVersion, invocationKind, idempotencyKey, inputDigest });
  if (!guarded.ok) return guarded;
  if (!INVOCATION_KINDS.includes(invocationKind)) return { ok: false, code: 'invocation_kind_invalid' };
  if (!AGENT_ROLES.includes(agentRole)) return { ok: false, code: 'agent_role_invalid' };
  if (typeof skillKey !== 'string' || !skillKey || skillKey.length > 64) {
    return { ok: false, code: 'skill_key_invalid' };
  }
  if (inputDigest != null && !isHexDigest(inputDigest)) return { ok: false, code: 'digest_invalid' };
  if (typeof idempotencyKey !== 'string' || !idempotencyKey || idempotencyKey.length > 200) {
    return { ok: false, code: 'idempotency_key_invalid' };
  }
  try {
    // 顺序：归属 → fail-closed 门 → 活跃门（门评估先于幂等重放——技能停用后，
    // 缓存式重放不得绕过门放行）；同键重放仅 RUNNING 视为「进行中」。
    const own = await validateOwnership(q, { tenantId, repoId, prId, runId, attemptId });
    if (!own.ok) return own;
    const failClosedCode = await checkFailClosedGates(q, { tenantId, run: own.run, invocationKind });
    const gate = await checkSkillGate(q, { tenantId, skillKey, skillVersion });
    const gateCode = failClosedCode
      ?? (gate.kind === 'inactive' ? 'skill_inactive'
        : gate.kind === 'version_mismatch' ? 'version_mismatch' : null);
    const existing = await findExisting(q, { tenantId, idempotencyKey });
    if (existing) {
      if (gateCode) {
        return { ...skillEventResult(existing), ok: false, code: gateCode };
      }
      if (existing.status !== 'RUNNING') {
        // 已终态事件=同一逻辑调用已完成（含门拒绝史）——不可再次执行（重试=新键）
        return { ...skillEventResult(existing), ok: false,
          code: existing.error_code ?? 'event_terminal' };
      }
      return skillEventResult(existing, { idempotent: true });
    }
    if (gateCode) {
      // 门拒绝：直接落终态 FAILED 事件（有痕、不放行——绝不伪造成功）
      const r = await q(
        `INSERT INTO mu.skill_invocation_event
           (tenant_id, repo_id, pr_id, run_id, attempt_id, agent_role, skill_key,
            skill_version, invocation_kind, status, started_at, completed_at,
            latency_ms, input_digest, error_code, idempotency_key)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'FAILED',now(),now(),0,$10,$11,$12)
         ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
         RETURNING *`,
        [tenantId, repoId, prId, runId, attemptId, agentRole, skillKey,
          gate.boundVersion, invocationKind, inputDigest, gateCode, idempotencyKey]);
      const row = r.rows[0] ?? await findExisting(q, { tenantId, idempotencyKey });
      return { ...(row ? skillEventResult(row) : {}), ok: false, code: gateCode };
    }
    const ins = await q(
      `INSERT INTO mu.skill_invocation_event
         (tenant_id, repo_id, pr_id, run_id, attempt_id, agent_role, skill_key,
          skill_version, invocation_kind, status, input_digest, idempotency_key)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'RUNNING',$10,$11)
       ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
       RETURNING *`,
      [tenantId, repoId, prId, runId, attemptId, agentRole, skillKey,
        gate.boundVersion, invocationKind, inputDigest, idempotencyKey]);
    const row = ins.rows[0] ?? await findExisting(q, { tenantId, idempotencyKey });
    return skillEventResult(row, { idempotent: ins.rows.length === 0, skillVersion: gate.boundVersion });
  } catch (e) {
    return { ok: false, code: 'recorder_db_error', detail: String(e?.code ?? '').slice(0, 16) };
  }
}

/**
 * Skill 调用结束：RUNNING→终态一次流转（CAS），latency 服务端按 started_at 结算。
 * eventId 或 (tenantId+idempotencyKey) 二选一寻址；已终态事件 → 拒绝（不可变）。
 * evidence-only（run.review_mode='evidence_only'）下 SUCCEEDED 被拒绝
 * （code='evidence_only_success_rejected'，零外部调用成功记录）。
 */
export async function recordSkillInvocationFinish(pool, { eventId = null, tenantId = null,
  idempotencyKey = null, status, outputDigest = null, errorCode = null }) {
  const q = (text, params) => pool.query(text, params);
  const guarded = guardInvocationMetadata({ eventId, tenantId, idempotencyKey, status,
    outputDigest, errorCode });
  if (!guarded.ok) return guarded;
  if (!TERMINAL_INVOCATION_STATUSES.has(status)) return { ok: false, code: 'invalid_finish_status' };
  if (outputDigest != null && !isHexDigest(outputDigest)) return { ok: false, code: 'digest_invalid' };
  if (errorCode != null && String(errorCode).length > 120) {
    errorCode = String(errorCode).slice(0, 120);
  }
  try {
    let ev = null;
    if (eventId) {
      ev = (await q(
        `SELECT e.*, r.review_mode FROM mu.skill_invocation_event e
           JOIN mu.review_run r ON r.run_id = e.run_id
          WHERE e.event_id=$1`, [eventId])).rows[0] ?? null;
    } else if (tenantId && idempotencyKey) {
      ev = (await q(
        `SELECT e.*, r.review_mode FROM mu.skill_invocation_event e
           JOIN mu.review_run r ON r.run_id = e.run_id
          WHERE e.tenant_id=$1 AND e.idempotency_key=$2`, [tenantId, idempotencyKey])).rows[0] ?? null;
    } else {
      return { ok: false, code: 'event_not_found' };
    }
    if (!ev) return { ok: false, code: 'event_not_found' };
    if (ev.status !== 'RUNNING') {
      return { ok: false, code: 'event_immutable', event: projectSkillEvent(ev) };
    }
    if (status === 'SUCCEEDED' && String(ev.review_mode ?? '') === 'evidence_only') {
      // evidence-only：不允许外部调用成功记录（调用方改记 FAILED+evidence_only_no_external_calls）
      return { ok: false, code: 'evidence_only_success_rejected', eventId: ev.event_id };
    }
    const r = await q(
      `UPDATE mu.skill_invocation_event
          SET status=$2, completed_at=now(),
              latency_ms=GREATEST(0, (EXTRACT(EPOCH FROM (now() - started_at)) * 1000)::int),
              output_digest=$3, error_code=$4
        WHERE event_id=$1 AND status='RUNNING'
        RETURNING *`, [ev.event_id, status, outputDigest, errorCode]);
    if (!r.rows.length) {
      const cur = await q(`SELECT * FROM mu.skill_invocation_event WHERE event_id=$1`, [ev.event_id]);
      return { ok: false, code: 'event_immutable', event: projectSkillEvent(cur.rows[0] ?? null) };
    }
    return { ok: true, event: projectSkillEvent(r.rows[0]) };
  } catch (e) {
    return { ok: false, code: 'recorder_db_error', detail: String(e?.code ?? '').slice(0, 16) };
  }
}

/**
 * 失败/超时/取消统一入口（FAILED/TIMEOUT/CANCELLED；脱敏 error_code 截 120）。
 * 事件不存在/已终态 → {ok:false, code}（retry 语义=新事件新键，不改历史）。
 */
export async function recordInvocationFailure(pool, { eventId = null, tenantId = null,
  idempotencyKey = null, status = 'FAILED', errorCode, outputDigest = null }) {
  if (!['FAILED', 'TIMEOUT', 'CANCELLED'].includes(status)) {
    return { ok: false, code: 'invalid_failure_status' };
  }
  const code = errorCode == null ? null : String(errorCode).slice(0, 120);
  return recordSkillInvocationFinish(pool, { eventId, tenantId, idempotencyKey,
    status, errorCode: code, outputDigest });
}

/**
 * RAG 检索留痕（单条；query 只以 64-hex digest 入库——原文绝不落库）。
 * run 可空（用户会话域检索无 run 上下文——不虚构）；run 存在时过复合 FK + 归属校验。
 * skill 活跃门同 Skill 路径：注册且停用 → 记 FAILED+skill_inactive 并返回 ok:false。
 * 幂等同 (tenant, idempotency_key)。
 */
export async function recordRagRetrieval(pool, { tenantId, repoId, prId = null, runId = null,
  attemptId = null, agentRole = 'system', skillKey = 'rag.retrieve', queryDigest,
  resultCount = 0, sourceDigestList = [], status = 'SUCCEEDED', errorCode = null,
  latencyMs = 0, idempotencyKey }) {
  const q = (text, params) => pool.query(text, params);
  const guarded = guardInvocationMetadata({ tenantId, repoId, prId, runId, attemptId,
    agentRole, skillKey, queryDigest, sourceDigestList, status, errorCode, idempotencyKey });
  if (!guarded.ok) return guarded;
  if (!AGENT_ROLES.includes(agentRole)) return { ok: false, code: 'agent_role_invalid' };
  if (!isHexDigest(queryDigest, 64, 64)) return { ok: false, code: 'digest_invalid' };
  if (!Array.isArray(sourceDigestList) || sourceDigestList.length > 64
    || !sourceDigestList.every((d) => isHexDigest(d, 8, 64))) {
    return { ok: false, code: 'digest_invalid' };
  }
  const count = Math.max(0, Math.min(Number(resultCount) || 0, 1_000_000));
  if (typeof idempotencyKey !== 'string' || !idempotencyKey || idempotencyKey.length > 200) {
    return { ok: false, code: 'idempotency_key_invalid' };
  }
  if (!['SUCCEEDED', 'FAILED', 'TIMEOUT', 'CANCELLED'].includes(status)) {
    return { ok: false, code: 'invalid_status' };
  }
  try {
    // 顺序：归属 → 门（先于幂等重放——技能停用后缓存式重放不得绕过门继续放行）
    const own = await validateOwnership(q, { tenantId, repoId, prId, runId, attemptId });
    if (!own.ok) return own;
    const failClosedCode = await checkFailClosedGates(q, { tenantId, run: own.run,
      invocationKind: 'rag_query' });
    const gate = await checkSkillGate(q, { tenantId, skillKey, skillVersion: null });
    const gateCode = failClosedCode
      ?? (gate.kind === 'inactive' ? 'skill_inactive'
        : gate.kind === 'version_mismatch' ? 'version_mismatch' : null);
    const existing = (await q(
      `SELECT * FROM mu.rag_retrieval_event WHERE tenant_id=$1 AND idempotency_key=$2`,
      [tenantId, idempotencyKey])).rows[0] ?? null;
    if (existing) {
      if (gateCode) {
        // 门现值拒绝：如实拒绝（历史行不动——记录的是「拒绝执行」，非伪造成功）
        return { ok: false, code: gateCode, eventId: existing.event_id,
          event: projectRagEvent(existing) };
      }
      return { ok: true, idempotent: true, eventId: existing.event_id,
        event: projectRagEvent(existing) };
    }
    const finalStatus = gateCode ? 'FAILED' : status;
    const finalError = gateCode ?? (errorCode == null ? null : String(errorCode).slice(0, 120));
    const r = await q(
      `INSERT INTO mu.rag_retrieval_event
         (tenant_id, repo_id, pr_id, run_id, attempt_id, agent_role, skill_key,
          query_digest, result_count, source_digest_list, status, started_at,
          completed_at, latency_ms, error_code, idempotency_key)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,now(),
         CASE WHEN $11 = 'SUCCEEDED' THEN now() END,
         $12,$13,$14)
       ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
       RETURNING *`,
      [tenantId, repoId, prId, runId, attemptId, agentRole, skillKey,
        queryDigest, count, JSON.stringify(sourceDigestList), finalStatus,
        Math.max(0, Number(latencyMs) || 0), finalError, idempotencyKey]);
    const row = r.rows[0] ?? (await q(
      `SELECT * FROM mu.rag_retrieval_event WHERE tenant_id=$1 AND idempotency_key=$2`,
      [tenantId, idempotencyKey])).rows[0];
    const okResult = finalStatus === 'SUCCEEDED';
    return { ok: okResult, ...(gateCode ? { code: gateCode } : {}),
      eventId: row.event_id, event: projectRagEvent(row) };
  } catch (e) {
    return { ok: false, code: 'recorder_db_error', detail: String(e?.code ?? '').slice(0, 16) };
  }
}

/**
 * executor 崩溃恢复：仍 RUNNING 但 (a) started_at 早于 olderThanMs，或 (b) 其
 * attempt/run 已到终态（fixer 卡死/worker 崩溃遗留）的事件 → 补记 INTERRUPTED。
 * 只触碰 RUNNING 行（不改任何已终态）；管线启动处挂一次调用。
 * 返回 {skill, rag}（各表补记条数）。
 */
export async function recoverIncompleteInvocations(pool, { olderThanMs = 15 * 60_000, limit = 200 } = {}) {
  const q = (text, params) => pool.query(text, params);
  const secs = Math.max(1, Number(olderThanMs) || 900_000) / 1000;
  const cap = Math.max(1, Math.min(Number(limit) || 200, 1000));
  const recover = async (table) => {
    const r = await q(
      `UPDATE mu.${table} e SET status='INTERRUPTED', completed_at=now(),
          latency_ms=GREATEST(0, (EXTRACT(EPOCH FROM (now() - e.started_at)) * 1000)::int),
          error_code='recovered_interrupted'
        WHERE e.status='RUNNING' AND e.event_id IN (
          SELECT e2.event_id FROM mu.${table} e2
            LEFT JOIN mu.agent_attempt a ON a.attempt_id = e2.attempt_id
            LEFT JOIN mu.review_run r2 ON r2.run_id = e2.run_id
           WHERE e2.status='RUNNING'
             AND ( e2.started_at < now() - make_interval(secs => $1)
                OR (e2.attempt_id IS NOT NULL AND a.status IS NOT NULL
                     AND a.status IN ('DONE','FAILED','TIMEOUT','SKIPPED'))
                OR (r2.status IS NOT NULL
                     AND r2.status IN ('FAILED','COMPLETED','BLOCKED')) )
           LIMIT $2)
        RETURNING e.event_id`, [secs, cap]);
    return r.rows.length;
  };
  try {
    const skill = await recover('skill_invocation_event');
    const rag = await recover('rag_retrieval_event');
    return { ok: true, skill, rag };
  } catch (e) {
    return { ok: false, code: 'recorder_db_error', detail: String(e?.code ?? '').slice(0, 16) };
  }
}