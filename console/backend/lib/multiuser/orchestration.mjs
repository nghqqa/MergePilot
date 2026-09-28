// console/backend/lib/multiuser/orchestration.mjs — Wave 3 PR-A：审查编排状态机。
// 数据模型见 schema.mjs migration 7（review_run/agent_attempt/agent_finding/
// fix_attempt/verification_attempt/orchestration_decision/dead_letter）。
//
// 安全与并发契约（对应任务书第三节）：
//  * 状态迁移 = DB CAS（UPDATE ... WHERE status = ANY(expected)）——并发消费只有一个
//    赢家；输家得到 {ok:false, reason:'cas_conflict'}，绝不重放副作用；
//  * 同 (tenant,repo,pr,head_sha) 幂等建 run（UNIQUE + ON CONFLICT）——重复 webhook/
//    手动触发拿到同一 run；新 head_sha 天然建新 run，旧 head 结果不被覆盖；
//  * attempt 编号原子递增（CTE max+1 + UNIQUE 冲突兜底）；超过 maxAttempts 拒绝，
//    由调用方转 dead_letter（重试有限）；
//  * 复合 FK 在数据库层拒绝跨 tenant/repo/pr 组合；
//  * 本模块不信任任何客户端提交的 tenant_id/repo_id/pr_id/head_sha——全部由调用方
//    （webhook 解析或服务端解析）提供；模块只做参数化 SQL；
//  * dead_letter 只写 payload_ref（引用），不写 payload 正文/代码/diff；
//  * 纯数据层：不 import provider/fixture；MU_FIXTURES 开关无关（真实路径可用）。
import crypto from 'node:crypto';

export const RUN_STATES = Object.freeze([
  'RECEIVED', 'REVIEW_QUEUED', 'REVIEWING', 'REVIEWED',
  'FIX_QUEUED', 'FIXING', 'VERIFY_QUEUED', 'VERIFYING', 'VERIFIED',
  'REWORK_REQUIRED', 'BLOCKED', 'FAILED', 'COMPLETED',
]);
export const TERMINAL_STATES = Object.freeze(new Set(['FAILED', 'COMPLETED']));

// 合法迁移表（JS 侧先验 + DB CHECK 兜底；BLOCKED 为半终态：人工裁定后可关闭）
export const RUN_TRANSITIONS = Object.freeze({
  RECEIVED: ['REVIEW_QUEUED', 'BLOCKED', 'FAILED'],
  REVIEW_QUEUED: ['REVIEWING', 'BLOCKED', 'FAILED'],
  REVIEWING: ['REVIEWED', 'BLOCKED', 'FAILED'],
  REVIEWED: ['FIX_QUEUED', 'VERIFY_QUEUED', 'COMPLETED', 'BLOCKED', 'FAILED'],
  FIX_QUEUED: ['FIXING', 'BLOCKED', 'FAILED'],
  FIXING: ['VERIFY_QUEUED', 'REWORK_REQUIRED', 'BLOCKED', 'FAILED'],
  VERIFY_QUEUED: ['VERIFYING', 'BLOCKED', 'FAILED'],
  VERIFYING: ['VERIFIED', 'BLOCKED', 'FAILED'],
  VERIFIED: ['COMPLETED', 'REWORK_REQUIRED', 'BLOCKED', 'FAILED'],
  REWORK_REQUIRED: ['FIX_QUEUED', 'BLOCKED', 'FAILED'],
  BLOCKED: ['COMPLETED', 'FAILED'],
  FAILED: [],
  COMPLETED: [],
});

const Q = (pool, text, params) => pool.query(text, params);

/** 幂等建 run：同 (tenant,repo,pr,head_sha) 返回既有 run（created=false）。 */
export async function createRunIfAbsent(pool, { tenantId, repoId, prId, headSha,
  triggerSource = 'webhook', requestedBy = null, policyVersion = 'v1' }) {
  const r = await Q(pool,
    `INSERT INTO mu.review_run (tenant_id, repo_id, pr_id, head_sha, trigger_source, requested_by, policy_version)
     VALUES ($1,$2,$3,$4,$5,$6,$7)
     ON CONFLICT (tenant_id, repo_id, pr_id, head_sha) DO NOTHING
     RETURNING *`,
    [tenantId, repoId, prId, headSha, triggerSource, requestedBy, policyVersion]);
  if (r.rows.length) return { run: r.rows[0], created: true };
  const ex = await Q(pool,
    `SELECT * FROM mu.review_run WHERE tenant_id=$1 AND repo_id=$2 AND pr_id=$3 AND head_sha=$4`,
    [tenantId, repoId, prId, headSha]);
  return { run: ex.rows[0] ?? null, created: false };
}

export async function getRun(pool, runId) {
  const r = await Q(pool, `SELECT * FROM mu.review_run WHERE run_id=$1`, [runId]);
  return r.rows[0] ?? null;
}

/** 当前 PR 最新 run（前端"当前阶段"查询用；按 created_at 倒序取首条）。 */
export async function getLatestRunForPr(pool, { tenantId, repoId, prId }) {
  const r = await Q(pool,
    `SELECT * FROM mu.review_run WHERE tenant_id=$1 AND repo_id=$2 AND pr_id=$3
      ORDER BY created_at DESC LIMIT 1`, [tenantId, repoId, prId]);
  return r.rows[0] ?? null;
}

/**
 * CAS 状态迁移：仅当当前 status ∈ expected 时迁到 next。
 * 返回 {ok:true, run} 或 {ok:false, reason:'invalid_transition'|'cas_conflict'|'not_found'}。
 */
export async function transitionRun(pool, { runId, from, to }) {
  if (!RUN_TRANSITIONS[from]?.includes(to)) return { ok: false, reason: 'invalid_transition' };
  const r = await Q(pool,
    `UPDATE mu.review_run SET status=$2, updated_at=now()
      WHERE run_id=$1 AND status = ANY($3::text[])
      RETURNING *`, [runId, to, from]);
  if (!r.rows.length) {
    const cur = await getRun(pool, runId);
    if (!cur) return { ok: false, reason: 'not_found' };
    return { ok: false, reason: 'cas_conflict', current: cur.status };
  }
  return { ok: true, run: r.rows[0] };
}

/**
 * 原子领取下一 attempt 编号（并发安全：CTE max+1 + UNIQUE 冲突兜底）。
 * 超过 maxAttempts 返回 {ok:false, reason:'max_attempts'}——调用方应转 dead_letter。
 */
export async function claimNextAttempt(pool, { runId, agentRole, provider,
  actorPrincipal = 'system:leader', inputDigest = null, maxAttempts = 3,
  tenantId, repoId, prId, headSha }) {
  const r = await Q(pool,
    `WITH next AS (
       SELECT coalesce(max(attempt), 0) + 1 AS n
         FROM mu.agent_attempt WHERE run_id=$1 AND agent_role=$2
     )
     INSERT INTO mu.agent_attempt
       (run_id, agent_role, attempt, provider, actor_principal, input_digest,
        tenant_id, repo_id, pr_id, head_sha)
     SELECT $1, $2, next.n, $3, $4, $5, $6, $7, $8, $9 FROM next
      WHERE next.n <= $10
     ON CONFLICT (run_id, agent_role, attempt) DO NOTHING
     RETURNING attempt_id, attempt`,
    [runId, agentRole, provider, actorPrincipal, inputDigest,
      tenantId, repoId, prId, headSha, maxAttempts]);
  if (!r.rows.length) {
    const cur = await Q(pool,
      `SELECT coalesce(max(attempt),0) AS n FROM mu.agent_attempt WHERE run_id=$1 AND agent_role=$2`,
      [runId, agentRole]);
    if (Number(cur.rows[0].n) >= maxAttempts) return { ok: false, reason: 'max_attempts' };
    return { ok: false, reason: 'attempt_conflict' }; // 罕见竞态：重试调用即可
  }
  return { ok: true, attemptId: r.rows[0].attempt_id, attempt: Number(r.rows[0].attempt) };
}

export async function finishAttempt(pool, { attemptId, status, outputDigest = null,
  modelId = null, promptVersion = null, latencyMs = null, tokenCount = null,
  errorCode = null, evidenceRef = '' }) {
  const r = await Q(pool,
    `UPDATE mu.agent_attempt SET status=$2, output_digest=$3, model_id=$4, prompt_version=$5,
        latency_ms=$6, token_count=$7, error_code=$8, evidence_ref=$9, updated_at=now()
     WHERE attempt_id=$1 RETURNING status`,
    [attemptId, status, outputDigest, modelId, promptVersion, latencyMs, tokenCount, errorCode, evidenceRef]);
  return r.rows.length > 0;
}

/** findings 批量落库（幂等：同 run+rule+path+line 重复插入零新增行；含脱敏摘要列）。 */
export async function insertFindings(pool, { attemptId, runId, tenantId, repoId, prId, headSha, findings }) {
  if (!Array.isArray(findings) || !findings.length) return 0;
  const values = [];
  const params = [attemptId, runId, tenantId, repoId, prId, headSha];
  for (const f of findings.slice(0, 500)) {
    const b = params.length;
    values.push(`($1,$2,$3,$4,$5,$6,
      $${b + 1},$${b + 2},$${b + 3},$${b + 4},$${b + 5},$${b + 6},$${b + 7},$${b + 8},$${b + 9},$${b + 10})`);
    params.push(String(f.rule_id ?? '').slice(0, 120),
      ['P0', 'P1', 'P2', 'P3'].includes(f.severity) ? f.severity : 'P3',
      Math.min(Math.max(Number(f.confidence ?? 0), 0), 1),
      String(f.path ?? '').slice(0, 500), Number(f.line_start ?? 0) || null, Number(f.line_end ?? 0) || null,
      String(f.title ?? '').slice(0, 300), String(f.evidence_ref ?? '').slice(0, 300),
      String(f.remediation ?? '').slice(0, 1000),
      String(f.summary_masked ?? '').slice(0, 120));
  }
  const r = await Q(pool,
    `INSERT INTO mu.agent_finding (attempt_id, run_id, tenant_id, repo_id, pr_id, head_sha,
       rule_id, severity, confidence, path, line_start, line_end, title, evidence_ref, remediation, summary_masked)
     VALUES ${values.join(',')}
     ON CONFLICT (run_id, rule_id, path, line_start) DO NOTHING
     RETURNING finding_id`, params);
  return r.rows.length;
}

export async function recordDecision(pool, { runId, tenantId, repoId, prId, headSha,
  stage, decision, rationaleRef = '', actorPrincipal = 'system:leader', policyVersion = 'v1' }) {
  const r = await Q(pool,
    `INSERT INTO mu.orchestration_decision
       (run_id, tenant_id, repo_id, pr_id, head_sha, stage, decision, rationale_ref, actor_principal, policy_version)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING decision_id`,
    [runId, tenantId, repoId, prId, headSha, String(stage).slice(0, 80),
      String(decision).slice(0, 80), String(rationaleRef).slice(0, 300),
      actorPrincipal, policyVersion]);
  return r.rows[0].decision_id;
}

/** 转死信：只存引用（payload_ref），绝不存 payload 正文/代码/diff。 */
export async function moveToDeadLetter(pool, { runId = null, tenantId = null, repoId = null,
  prId = null, headSha = null, agentRole = null, jobId = null, kind, reason,
  retryCount = 0, payloadRef = null }) {
  const r = await Q(pool,
    `INSERT INTO mu.dead_letter (run_id, tenant_id, repo_id, pr_id, head_sha, agent_role,
        job_id, kind, reason, retry_count, payload_ref)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING dlq_id`,
    [runId, tenantId, repoId, prId, headSha, agentRole, jobId,
      String(kind).slice(0, 80), String(reason).slice(0, 300), retryCount,
      payloadRef ? String(payloadRef).slice(0, 300) : null]);
  return r.rows[0].dlq_id;
}

/** 死信查询（元数据列白名单——无 payload 正文字段）。 */
export async function listDeadLetter(pool, { limit = 50, unresolvedOnly = true } = {}) {
  const r = await Q(pool,
    `SELECT dlq_id, run_id, tenant_id, repo_id, pr_id, head_sha, agent_role, job_id,
            kind, reason, retry_count, payload_ref, created_at, resolved_at
       FROM mu.dead_letter
      ${unresolvedOnly ? 'WHERE resolved_at IS NULL' : ''}
      ORDER BY created_at DESC LIMIT $1`, [Math.min(Number(limit) || 50, 200)]);
  return r.rows;
}

export async function resolveDeadLetter(pool, dlqId) {
  const r = await Q(pool,
    `UPDATE mu.dead_letter SET resolved_at=now() WHERE dlq_id=$1 AND resolved_at IS NULL RETURNING dlq_id`,
    [dlqId]);
  return r.rows.length > 0;
}

/** digest 工具（input/output 指纹——记录用，不含正文）。 */
export const digestOf = (v) => crypto.createHash('sha256').update(String(v)).digest('hex');
