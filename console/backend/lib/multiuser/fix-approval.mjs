// console/backend/lib/multiuser/fix-approval.mjs — v16 高危修复审批门（fix/high-risk-fix-approval-gate）。
//
// 契约（任务书二/三/五/六节）：
//  * P0/P1 fix_required 一律先建【逐 finding】具名审批票（PENDING）+ run →
//    WAITING_FOR_HUMAN_APPROVAL；全部票 APPROVED 才进 FIX_QUEUED；任一 REJECTED/
//    EXPIRED/STALE → run BLOCKED（fail-closed，绝不进 Fixer）。
//  * 审批票只能由服务端从当前 run/finding 创建（ensureFixApprovals——tenant/severity/
//    head_sha/finding_id 全部服务端解析；客户端提交值一律不可信）。
//  * 决定绑定票据版本（CAS UPDATE ... WHERE status='PENDING'）：重复 decide 幂等冲突、
//    两 Maintainer 并发只有一个赢家；decision_digest=sha256(approval_id|status|decided_by|
//    decided_at) 防重放比对。
//  * authorizeFixExecution(runId, findingId)：所有 Fixer 入口的唯一硬门（v1 fix-orchestrator
//    + v2 fixer-sandbox 都在状态迁移/attempt 领取【之前】调用）；首过即消费（APPROVED→
//    CONSUMED，原子 CAS——并发入口只有一个消费成功；REWORK 回派轮经 CONSUMED 幂等放行，
//    单飞由 FIX_QUEUED→FIXING CAS 保证）。
//  * 任何决定不产生 GitHub approve/merge/write（本模块零 GitHub 调用）。
//  * 审计只含 id/severity/head 前缀/状态/决定人/digest——零 token/secret/原始代码/patch。
import crypto from 'node:crypto';
import { getRun, transitionRun, digestOf } from './orchestration.mjs';

export const FIX_APPROVAL_TTL_MS = Number(process.env.MU_FIX_APPROVAL_TTL_MS || 7 * 24 * 3600 * 1000);

// 稳定 reason code（任务书六节）
export const FIX_GATE_REASONS = Object.freeze({
  NO_APPROVAL: 'HIGH_RISK_APPROVAL_REQUIRED',
  PENDING: 'FIX_BLOCKED_APPROVAL_PENDING',
  REJECTED: 'FIX_APPROVAL_REJECTED',
  EXPIRED: 'FIX_APPROVAL_EXPIRED',
  STALE: 'FIX_APPROVAL_STALE',
  HEAD_MISMATCH: 'FIX_APPROVAL_HEAD_MISMATCH',
  CONSUMED: 'FIX_APPROVAL_ALREADY_CONSUMED',
});

const q = (pool, text, params) => pool.query(text, params);

async function audit(pool, tenantId, kind, detail) {
  await q(pool,
    `INSERT INTO mu.audit_event (tenant_id, actor_user_id, kind, detail)
     VALUES ($1, NULL, $2, $3::jsonb)`,
    [tenantId, kind, JSON.stringify(detail)]).catch(() => {});
}

/** 票据内容 digest（服务端计算；确定性——重复 webhook 重放同值） */
function ticketDigest({ findingId, headSha, severity, ruleId, path, lineStart }) {
  return digestOf([findingId, headSha, severity, ruleId, path, lineStart].join('|'));
}

/**
 * Leader 路径调用：为 run 的全部 P0/P1 finding 幂等创建 PENDING 审批票。
 * 重复调用/重复 webhook 零新增（活票唯一索引 + ON CONFLICT DO NOTHING）。
 * 返回 { tickets, created }。不迁移 run 状态（调用方负责 REVIEWED→WAITING）。
 */
export async function ensureFixApprovals(pool, { run, binding, prNumber = null }) {
  // pr_number 服务端解析（pull_request 域单源——调用方/客户端传值仅作显示回退，不作信任源）
  if (!prNumber) {
    const pr = await q(pool,
      `SELECT provider_pr_number FROM mu.pull_request
        WHERE tenant_id=$1 AND repo_id=$2 AND pr_id=$3`,
      [binding.tenantId, binding.repoId, binding.prId]);
    prNumber = pr.rows[0]?.provider_pr_number ?? '';
  }
  const findings = (await q(pool,
    `SELECT finding_id, severity, rule_id, path, line_start, head_sha
       FROM mu.agent_finding WHERE run_id=$1 AND severity IN ('P0','P1')`,
    [run.run_id])).rows;
  const created = [];
  for (const f of findings) {
    const diffDigest = ticketDigest({ findingId: f.finding_id, headSha: f.head_sha,
      severity: f.severity, ruleId: f.rule_id, path: f.path, lineStart: f.line_start });
    const r = await q(pool,
      `INSERT INTO mu.fix_approval
         (tenant_id, repo_id, pr_id, pr_number, run_id, finding_id, severity, head_sha,
          diff_digest, requested_by, expires_at, status)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'system:leader', now() + ($10::bigint * interval '1 millisecond'), 'PENDING')
       ON CONFLICT DO NOTHING
       RETURNING approval_id, expires_at`,
      [binding.tenantId, binding.repoId, binding.prId, String(prNumber ?? ''),
        run.run_id, f.finding_id, f.severity, f.head_sha ?? binding.headSha,
        diffDigest, FIX_APPROVAL_TTL_MS]);
    if (r.rows.length) {
      created.push(r.rows[0]);
      await audit(pool, binding.tenantId, 'FIX_APPROVAL_REQUESTED', {
        approval_id: r.rows[0].approval_id, run_id: run.run_id, finding_id: f.finding_id,
        severity: f.severity, head_prefix: String(f.head_sha ?? binding.headSha ?? '').slice(0, 12),
        expires_at: r.rows[0].expires_at, rule_id: f.rule_id, path: f.path,
      });
    }
  }
  if (created.length) {
    await audit(pool, binding.tenantId, 'FIX_BLOCKED_NO_APPROVAL', {
      run_id: run.run_id, created_tickets: created.length, total_high_risk: findings.length,
    });
  }
  const tickets = (await q(pool,
    `SELECT * FROM mu.fix_approval WHERE run_id=$1 ORDER BY severity, created_at`, [run.run_id])).rows;
  return { tickets, created: created.length };
}

/**
 * 审批决定（approve/reject）。CAS 绑定票据当前 PENDING 版本：
 *  - 赢家：APPROVED/REJECTED + decided_* + decision_digest；审计 FIX_APPROVED/FIX_REJECTED；
 *    approve 时若 run 已无 PENDING 票 → WAITING_FOR_HUMAN_APPROVAL→FIX_QUEUED（唯一进 FIX_QUEUED 的门）；
 *    reject → 票 REJECTED + run WAITING→BLOCKED。
 *  - 输家（非 PENDING）：幂等返回既有态（同向决定 200 ok:false? 不——返回 {ok:true, idempotent:true,
 *    status} 供 API 层区分 200/409），冲突方向由 API 层判 409。
 */
export async function decideFixApproval(pool, { approvalId, decision, decidedBy,
  decisionReason = null, tenantId }) {
  if (!['approve', 'reject'].includes(decision)) throw new Error('invalid_decision');
  const next = decision === 'approve' ? 'APPROVED' : 'REJECTED';
  const cur = (await q(pool, `SELECT * FROM mu.fix_approval WHERE approval_id=$1`, [approvalId])).rows[0] ?? null;
  if (!cur) return { ok: false, reason: 'not_found' };
  if (tenantId && cur.tenant_id !== tenantId) return { ok: false, reason: 'not_found' }; // 防枚举：跨租户按不存在处理

  if (cur.status !== 'PENDING') {
    // 重复决定：同向=幂等成功；反向/终局负态=409 冲突
    if (cur.status === next || (next === 'APPROVED' && cur.status === 'CONSUMED')) {
      return { ok: true, idempotent: true, ticket: cur };
    }
    return { ok: false, reason: 'conflict', status: cur.status };
  }
  const decidedAt = new Date();
  const decisionDigest = digestOf([approvalId, next, decidedBy, decidedAt.toISOString()].join('|'));
  const upd = await q(pool,
    `UPDATE mu.fix_approval SET status=$2, decided_by=$3, decided_at=$4,
        decision_reason=$5, decision_digest=$6
      WHERE approval_id=$1 AND status='PENDING'
      RETURNING *`,
    [approvalId, next, String(decidedBy).slice(0, 160), decidedAt,
      decisionReason ? String(decisionReason).slice(0, 500) : null, decisionDigest]);
  if (!upd.rows.length) {
    // 并发输家：重读终局
    const again = (await q(pool, `SELECT * FROM mu.fix_approval WHERE approval_id=$1`, [approvalId])).rows[0];
    return (again.status === next || (next === 'APPROVED' && again.status === 'CONSUMED'))
      ? { ok: true, idempotent: true, ticket: again }
      : { ok: false, reason: 'conflict', status: again.status };
  }
  const ticket = upd.rows[0];
  await audit(pool, ticket.tenant_id, next === 'APPROVED' ? 'FIX_APPROVED' : 'FIX_REJECTED', {
    approval_id: approvalId, run_id: ticket.run_id, finding_id: ticket.finding_id,
    decided_by: decidedBy, decision_digest: decisionDigest,
    ...(decisionReason ? { decision_reason: String(decisionReason).slice(0, 200) } : {}),
  });

  if (next === 'REJECTED') {
    await transitionRun(pool, { runId: ticket.run_id,
      from: ['WAITING_FOR_HUMAN_APPROVAL', 'REVIEWED'], to: 'BLOCKED' }).catch(() => {});
    return { ok: true, ticket, run_blocked: true };
  }
  // approve：仅当该 run 再无 PENDING/负态票（全部高危已批准）才放行 FIX_QUEUED
  const pending = await q(pool,
    `SELECT count(*)::int AS c FROM mu.fix_approval
      WHERE run_id=$1 AND status='PENDING'`, [ticket.run_id]);
  if (Number(pending.rows[0].c) === 0) {
    const t = await transitionRun(pool, { runId: ticket.run_id,
      from: ['WAITING_FOR_HUMAN_APPROVAL', 'REVIEWED'], to: 'FIX_QUEUED' });
    return { ok: true, ticket, run_ready: t.ok, run_state: t.ok ? 'FIX_QUEUED' : (t.current ?? null) };
  }
  return { ok: true, ticket, run_ready: false, run_state: 'WAITING_FOR_HUMAN_APPROVAL' };
}

/**
 * 惰性清收（读路径与 gate 前调用）：
 *  - PENDING 且 now()>expires_at → EXPIRED（审计 FIX_APPROVAL_EXPIRED）+ run WAITING→BLOCKED；
 *  - PENDING 且同 (tenant,repo,pr) 已有更新的不同 head run → STALE（审计 FIX_APPROVAL_STALE）
 *    + run WAITING→BLOCKED；
 *  - PENDING 且 run 已离开 WAITING/REVIEWED（异常态）→ STALE。
 *  过期/STALE 后永不自动回 FIX_QUEUED（任务书七.7）。
 */
export async function sweepFixApprovals(pool) {
  let changed = 0;
  const expired = (await q(pool,
    `UPDATE mu.fix_approval SET status='EXPIRED'
      WHERE status='PENDING' AND expires_at < now() RETURNING *`)).rows;
  for (const t of expired) {
    changed++;
    await audit(pool, t.tenant_id, 'FIX_APPROVAL_EXPIRED', { approval_id: t.approval_id, run_id: t.run_id });
    await transitionRun(pool, { runId: t.run_id, from: ['WAITING_FOR_HUMAN_APPROVAL'], to: 'BLOCKED' }).catch(() => {});
  }
  const stale = (await q(pool,
    `UPDATE mu.fix_approval fa SET status='STALE'
      WHERE fa.status='PENDING' AND (
        EXISTS (SELECT 1 FROM mu.review_run newer
                 WHERE newer.tenant_id=fa.tenant_id AND newer.repo_id=fa.repo_id AND newer.pr_id=fa.pr_id
                   AND newer.head_sha <> fa.head_sha AND newer.created_at > fa.created_at)
        OR EXISTS (SELECT 1 FROM mu.review_run r
                    WHERE r.run_id=fa.run_id AND r.status NOT IN ('WAITING_FOR_HUMAN_APPROVAL','REVIEWED'))
      ) RETURNING *`)).rows;
  for (const t of stale) {
    changed++;
    await audit(pool, t.tenant_id, 'FIX_APPROVAL_STALE', { approval_id: t.approval_id, run_id: t.run_id });
    await transitionRun(pool, { runId: t.run_id, from: ['WAITING_FOR_HUMAN_APPROVAL'], to: 'BLOCKED' }).catch(() => {});
  }
  return { changed };
}

/**
 * 唯一调度硬门（任务书五节）——所有 Fixer 入口在状态迁移/attempt 领取之前调用。
 * 返回 {ok:true, ticket} 或 {ok:false, reason, detail?}（reason ∈ FIX_GATE_REASONS /
 * 'finding_not_high_risk' / 'run_not_found' / 'finding_not_found' / 'run_in_terminal_state' /
 * 'egress_consent_revoked' / 'run_not_fixable_state'）。
 */
export async function authorizeFixExecution(pool, { runId, findingId, consume = true }) {
  const run = await getRun(pool, runId);
  if (!run) return { ok: false, reason: 'run_not_found' };
  if (['FAILED', 'COMPLETED'].includes(run.status)) {
    return { ok: false, reason: 'run_in_terminal_state', detail: { status: run.status } };
  }
  const finding = (await q(pool,
    `SELECT finding_id, severity, rule_id, path, line_start, head_sha FROM mu.agent_finding
      WHERE finding_id=$1 AND run_id=$2`, [findingId, runId])).rows[0] ?? null;
  if (!finding) return { ok: false, reason: 'finding_not_found' };
  if (!['P0', 'P1'].includes(finding.severity)) {
    return { ok: false, reason: 'finding_not_high_risk', detail: { severity: finding.severity } };
  }
  await sweepFixApprovals(pool); // gate 前清收（过期/STALE 即时生效）

  // run 级门：任一 P0/P1 票未批准 → 全 run 不放行（逐条审批，无"全部批准"捷径）
  const tickets = (await q(pool,
    `SELECT * FROM mu.fix_approval WHERE run_id=$1 AND severity IN ('P0','P1')`, [runId])).rows;
  if (!tickets.length) return { ok: false, reason: FIX_GATE_REASONS.NO_APPROVAL };
  const bad = tickets.find((t) => ['PENDING', 'REJECTED', 'EXPIRED', 'STALE'].includes(t.status));
  if (bad) {
    return { ok: false, reason: bad.status === 'PENDING' ? FIX_GATE_REASONS.PENDING
      : bad.status === 'REJECTED' ? FIX_GATE_REASONS.REJECTED
      : bad.status === 'EXPIRED' ? FIX_GATE_REASONS.EXPIRED : FIX_GATE_REASONS.STALE,
      detail: { approval_id: bad.approval_id, status: bad.status } };
  }

  // 本 finding 的票：绑定校验（tenant/head/digest/finding 一致——不匹配即 STALE 语义拒绝）
  const mine = tickets.find((t) => t.finding_id === String(findingId))
    ?? tickets.find((t) => String(t.finding_id) === String(finding.finding_id));
  if (!mine) return { ok: false, reason: FIX_GATE_REASONS.NO_APPROVAL, detail: { finding_id: String(findingId) } };
  if (mine.tenant_id !== run.tenant_id) return { ok: false, reason: FIX_GATE_REASONS.HEAD_MISMATCH, detail: { kind: 'tenant' } };
  if (mine.head_sha !== run.head_sha) return { ok: false, reason: FIX_GATE_REASONS.HEAD_MISMATCH, detail: { kind: 'head_sha' } };
  const expect = ticketDigest({ findingId: finding.finding_id, headSha: finding.head_sha,
    severity: finding.severity, ruleId: finding.rule_id, path: finding.path, lineStart: finding.line_start });
  if (mine.diff_digest !== expect) return { ok: false, reason: FIX_GATE_REASONS.STALE, detail: { kind: 'diff_digest' } };

  if (mine.status === 'CONSUMED') {
    // REWORK 回派轮：已消费票幂等放行（单飞由 FIX_QUEUED→FIXING CAS 保证；不重复消费）
    return { ok: true, ticket: mine, reconsumed: true };
  }
  if (mine.status !== 'APPROVED') {
    return { ok: false, reason: FIX_GATE_REASONS.PENDING, detail: { status: mine.status } };
  }
  if (!consume) return { ok: true, ticket: mine };
  // 原子消费（APPROVED→CONSUMED）：并发入口只有一个赢家
  const upd = await q(pool,
    `UPDATE mu.fix_approval SET status='CONSUMED', consumed_at=now()
      WHERE approval_id=$1 AND status='APPROVED' RETURNING *`, [mine.approval_id]);
  if (!upd.rows.length) {
    const again = (await q(pool, `SELECT status FROM mu.fix_approval WHERE approval_id=$1`, [mine.approval_id])).rows[0];
    return (again?.status === 'CONSUMED')
      ? { ok: true, ticket: { ...mine, status: 'CONSUMED' }, reconsumed: true }
      : { ok: false, reason: FIX_GATE_REASONS.CONSUMED, detail: { status: again?.status } };
  }
  await audit(pool, mine.tenant_id, 'FIX_STARTED_AFTER_APPROVAL', {
    approval_id: mine.approval_id, run_id: runId, finding_id: String(findingId),
    decided_by: mine.decided_by, decision_digest: mine.decision_digest,
  });
  return { ok: true, ticket: upd.rows[0] };
}

/** 读路径：租户内审批票列表（含 finding 摘要联查；供 API/前端）。 */
export async function listFixApprovals(pool, { tenantId, status = null, runId = null, limit = 100 }) {
  const params = [tenantId];
  let where = `fa.tenant_id=$1`;
  if (status) { params.push(String(status)); where += ` AND fa.status=$${params.length}`; }
  if (runId) { params.push(String(runId)); where += ` AND fa.run_id=$${params.length}`; }
  params.push(Math.min(Number(limit) || 100, 200));
  const r = await q(pool,
    `SELECT fa.approval_id, fa.tenant_id, fa.repo_id, fa.pr_id, fa.pr_number, fa.run_id,
            fa.finding_id, fa.severity, fa.head_sha, fa.status, fa.requested_action,
            fa.created_at, fa.expires_at, fa.decided_by, fa.decided_at, fa.decision_reason,
            f.rule_id, f.path, f.line_start, f.summary_masked,
            r.owner AS repo_owner, r.name AS repo_name,
            rr.status AS run_status
       FROM mu.fix_approval fa
       JOIN mu.agent_finding f ON f.finding_id = fa.finding_id
       JOIN mu.repository r ON r.repo_id = fa.repo_id
       JOIN mu.review_run rr ON rr.run_id = fa.run_id
      WHERE ${where}
      ORDER BY fa.created_at DESC LIMIT $${params.length}`, params);
  return r.rows;
}
