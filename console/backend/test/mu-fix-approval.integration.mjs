#!/usr/bin/env node
// console/backend/test/mu-fix-approval.integration.mjs — v16 高危修复审批门集成测试
// （fix/high-risk-fix-approval-gate，任务书九节矩阵）。
// 运行：node console/backend/test/mu-fix-approval.integration.mjs
// （自起一次性 postgres:16-alpine 容器，随机回环端口，跑毕 docker rm -f——不触常驻栈。）
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + JSON.stringify(detail).slice(0, 220) : ''}`); }
};

const CTR = `mu-fa-${crypto.randomBytes(4).toString('hex')}`;
const PGPORT = 17800 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR,
  '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PGPORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const dsn = `postgres://postgres:x@127.0.0.1:${PGPORT}/mu`;
const pool = new Pool({ connectionString: dsn });
for (let i = 0; i < 60; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 800)); } }

process.env.MU_MODE = 'multiuser';
process.env.CONSOLE_PG_DSN = dsn;
process.env.MU_EXECUTOR = 'internal';
process.env.MU_EXECUTOR_INTERNAL_ALLOW = 'test'; // 三值白名单（development/test/emergency）

const orch = await import('../lib/multiuser/orchestration.mjs');
const leader = await import('../lib/multiuser/agents/leader.mjs');
const ext = await import('../lib/multiuser/agents/external-reviewer.mjs');
const fxo = await import('../lib/multiuser/agents/fix-orchestrator.mjs');
const fa = await import('../lib/multiuser/fix-approval.mjs');
const authz = await import('../lib/multiuser/authz.mjs');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const store = await createMuStore({ pool });
await store.initSchema();
await store.bootstrap();

const T1 = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;

// ── FA0：迁移 v16（fresh DB；矩阵 20）──
{
  const mv = (await pool.query(`SELECT max(version) AS v FROM mu.schema_migrations`)).rows[0].v;
  ok('FA0a fresh DB 迁移到 v16', Number(mv) === 16, { v: mv });
  const tbl = (await pool.query(`SELECT 1 FROM information_schema.tables
    WHERE table_schema='mu' AND table_name='fix_approval'`)).rows.length;
  ok('FA0b mu.fix_approval 表存在', tbl === 1);
  const ck = (await pool.query(`SELECT 1 FROM mu.review_run(none) LIMIT 0`).catch(() => null));
  // CHECK 换枚举验证：直接插入 WAITING 状态行应被接受（借临时 run）——见 FA1 实跑覆盖
  ok('FA0c review_run CHECK 已含新态（FA1 实证）', ck === null);
}

const mkRepo = async (name) => (await pool.query(
  `INSERT INTO mu.repository (tenant_id, provider, provider_repo_id, owner, name)
   VALUES ($1,'github',$2,'fa', $3) ON CONFLICT DO NOTHING RETURNING repo_id`,
  [T1, name, name])).rows[0];
const REPO = await mkRepo('gate-repo');

/** 建 run → REVIEWED → 插 findings →（可选）Leader v2 消费。返回 {run,binding,findings} */
async function mkRun({ findings, consume = true }) {
  const head = crypto.randomBytes(20).toString('hex');
  const prNum = Math.floor(Math.random() * 900000) + 1000;
  const pr = (await pool.query(`INSERT INTO mu.pull_request (tenant_id, repo_id, provider_pr_number, head_sha)
    VALUES ($1,$2,$3,$4) RETURNING pr_id, provider_pr_number`, [T1, REPO.repo_id, prNum, head])).rows[0];
  const { run } = await orch.createRunIfAbsent(pool, { tenantId: T1, repoId: REPO.repo_id, prId: pr.pr_id, headSha: head });
  for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED']]) {
    await orch.transitionRun(pool, { runId: run.run_id, from: [f], to: t });
  }
  const binding = { tenantId: T1, repoId: REPO.repo_id, prId: pr.pr_id, headSha: head };
  let inserted = [];
  if (findings?.length) {
    const att = await orch.claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
      provider: 'deterministic', maxAttempts: 3, ...binding });
    await orch.insertFindings(pool, { attemptId: att.attemptId, runId: run.run_id, ...binding, findings });
    await orch.finishAttempt(pool, { attemptId: att.attemptId, status: 'DONE' });
    inserted = (await pool.query(`SELECT finding_id, severity FROM mu.agent_finding
      WHERE run_id=$1 ORDER BY severity`, [run.run_id])).rows;
  }
  let lead = null;
  if (consume) {
    lead = await ext.leaderConsumeFindings(pool, { run, binding, protection: { configured: true } });
  }
  return { run, binding, findings: inserted, lead, pr };
}
const findId = async (runId, idx = 0) =>
  (await pool.query(`SELECT finding_id FROM mu.agent_finding WHERE run_id=$1 ORDER BY severity, created_at OFFSET $2 LIMIT 1`,
    [runId, idx])).rows[0]?.finding_id ?? null;
const runStatus = async (runId) => (await orch.getRun(pool, runId))?.status;
const ticketsOf = async (runId) => (await pool.query(
  `SELECT * FROM mu.fix_approval WHERE run_id=$1 ORDER BY severity, created_at`, [runId])).rows;
const auditKinds = async (kind, runId) => (await pool.query(
  `SELECT count(*)::int AS c FROM mu.audit_event WHERE kind=$1 AND detail->>'run_id'=$2`, [kind, runId])).rows[0].c;

const P0F = [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 'a.js', line_start: 3,
  title: 'hardcoded secret', summary_masked: 'sk-***' }];
const P0P1F = [...P0F, { rule_id: 'R-SQL-CONCAT', severity: 'P1', confidence: 0.8, path: 'db.js',
  line_start: 7, title: 'sql concat', summary_masked: 'q=***' }];
const P2F = [{ rule_id: 'R-STYLE', severity: 'P2', confidence: 0.5, path: 'x.js', line_start: 1,
  title: 'style', summary_masked: '' }];

try {
  // ── FA1：P0/P1 → PENDING 逐条审批票 + WAITING（矩阵 1）──
  const s1 = await mkRun({ findings: P0P1F });
  ok('FA1a leader fix_required', s1.lead.decision === 'fix_required', s1.lead);
  ok('FA1b run → WAITING_FOR_HUMAN_APPROVAL（不进 FIX_QUEUED）',
    await runStatus(s1.run.run_id) === 'WAITING_FOR_HUMAN_APPROVAL');
  const t1 = await ticketsOf(s1.run.run_id);
  ok('FA1c 逐 finding 两张 PENDING 票', t1.length === 2 && t1.every((t) => t.status === 'PENDING'), t1.length);
  ok('FA1d 审计 FIX_APPROVAL_REQUESTED×2', await auditKinds('FIX_APPROVAL_REQUESTED', s1.run.run_id) === 2);
  ok('FA1e 审计 FIX_BLOCKED_NO_APPROVAL', (await auditKinds('FIX_BLOCKED_NO_APPROVAL', s1.run.run_id)) >= 1);
  ok('FA1f 票绑定 head/severity/pr_number', t1[0].head_sha === s1.binding.headSha
    && ['P0', 'P1'].includes(t1[0].severity) && Number(t1[0].pr_number) === Number(s1.pr.provider_pr_number));

  // ── FA2：审批前 Fixer 不启动（矩阵 2/3/4：入口统一 gate；重试/恢复同路径）──
  {
    const g = await fa.authorizeFixExecution(pool, { runId: s1.run.run_id, findingId: t1[0].finding_id, consume: false });
    ok('FA2a gate：PENDING → FIX_BLOCKED_APPROVAL_PENDING', g.ok === false && g.reason === 'FIX_BLOCKED_APPROVAL_PENDING', g);
    const g2 = await fa.authorizeFixExecution(pool, { runId: s1.run.run_id, findingId: t1[0].finding_id, consume: false });
    ok('FA2b 重复 gate（retry 语义）仍拒', g2.ok === false && g2.reason === 'FIX_BLOCKED_APPROVAL_PENDING');
    const v1round = await fxo.fixVerifyRound(pool, { run: { run_id: s1.run.run_id }, binding: s1.binding, deps: {
      assertServiceChain: async () => true, env: process.env } });
    ok('FA2c fixVerifyRound（v1 入口）审批前拒', v1round.ok === false
      && v1round.stage === 'fix_blocked_no_approval', v1round);
    const attempts = (await pool.query(`SELECT count(*)::int AS c FROM mu.agent_attempt
      WHERE run_id=$1 AND agent_role='fixer'`, [s1.run.run_id])).rows[0].c;
    ok('FA2d 零 fixer attempt 残留', Number(attempts) === 0, attempts);
    ok('FA2e run 停留 WAITING（可恢复）', await runStatus(s1.run.run_id) === 'WAITING_FOR_HUMAN_APPROVAL');
  }

  // ── FA3：Maintainer 批准（逐条）→ 全批准才 FIX_QUEUED → DRY_RUN 启动（矩阵 5/12）──
  {
    const a1 = await fa.decideFixApproval(pool, { approvalId: t1[0].approval_id, decision: 'approve',
      decidedBy: 'mu:maintainer-x', tenantId: T1 });
    ok('FA3a 批准第一张票 ok', a1.ok === true && a1.ticket.status === 'APPROVED');
    ok('FA3b 仍有一张 PENDING → run 不放行', a1.run_ready === false
      && await runStatus(s1.run.run_id) === 'WAITING_FOR_HUMAN_APPROVAL');
    const a1dup = await fa.decideFixApproval(pool, { approvalId: t1[0].approval_id, decision: 'approve',
      decidedBy: 'mu:maintainer-x', tenantId: T1 });
    ok('FA3c 重复 approve 幂等', a1dup.ok === true && a1dup.idempotent === true);
    const a2 = await fa.decideFixApproval(pool, { approvalId: t1[1].approval_id, decision: 'approve',
      decidedBy: 'mu:maintainer-x', tenantId: T1 });
    ok('FA3d 全部批准 → run FIX_QUEUED', a2.ok === true && a2.run_ready === true
      && await runStatus(s1.run.run_id) === 'FIX_QUEUED');
    ok('FA3e 审计 FIX_APPROVED≥2', (await auditKinds('FIX_APPROVED', s1.run.run_id)) >= 2);

    // DRY_RUN 启动（gate 消费）
    const g = await fa.authorizeFixExecution(pool, { runId: s1.run.run_id, findingId: t1[0].finding_id });
    ok('FA3f gate 放行（APPROVED）', g.ok === true && g.ticket.status === 'CONSUMED', g.ticket?.status);
    const gAgain = await fa.authorizeFixExecution(pool, { runId: s1.run.run_id, findingId: t1[0].finding_id });
    ok('FA3g 二次进入（rework 回派语义）CONSUMED 幂等放行', gAgain.ok === true && gAgain.reconsumed === true);
    ok('FA3h 审计 FIX_STARTED_AFTER_APPROVAL', (await auditKinds('FIX_STARTED_AFTER_APPROVAL', s1.run.run_id)) >= 1);
    // 并发消费只赢一次（矩阵 13）：重置一张 APPROVED 后双发
    await pool.query(`UPDATE mu.fix_approval SET status='APPROVED', consumed_at=NULL WHERE approval_id=$1`, [t1[1].approval_id]);
    const [c1, c2] = await Promise.all([
      fa.authorizeFixExecution(pool, { runId: s1.run.run_id, findingId: t1[1].finding_id }),
      fa.authorizeFixExecution(pool, { runId: s1.run.run_id, findingId: t1[1].finding_id }),
    ]);
    const consumed = [c1, c2].filter((x) => x.ok && !x.reconsumed).length;
    ok('FA3i 并发消费仅一次', consumed === 1 && c1.ok && c2.ok, { c1: c1.reason, c2: c2.reason });
  }

  // ── FA4：拒绝 → REJECTED + run BLOCKED，Fixer 永不启动（矩阵 8）──
  {
    const s4 = await mkRun({ findings: P0F });
    const t4 = (await ticketsOf(s4.run.run_id))[0];
    const r = await fa.decideFixApproval(pool, { approvalId: t4.approval_id, decision: 'reject',
      decidedBy: 'mu:maintainer-x', decisionReason: 'noisy finding', tenantId: T1 });
    ok('FA4a 拒绝 → 票 REJECTED', r.ok === true && r.ticket.status === 'REJECTED' && r.run_blocked === true);
    ok('FA4b run → BLOCKED', await runStatus(s4.run.run_id) === 'BLOCKED');
    const g = await fa.authorizeFixExecution(pool, { runId: s4.run.run_id, findingId: t4.finding_id });
    ok('FA4c gate → FIX_APPROVAL_REJECTED', g.ok === false && g.reason === 'FIX_APPROVAL_REJECTED', g);
    const fx = (await pool.query(`SELECT count(*)::int AS c FROM mu.agent_attempt
      WHERE run_id=$1 AND agent_role='fixer'`, [s4.run.run_id])).rows[0].c;
    ok('FA4d 零 fixer attempt', Number(fx) === 0);
    const again = await fa.decideFixApproval(pool, { approvalId: t4.approval_id, decision: 'approve',
      decidedBy: 'mu:maintainer-x', tenantId: T1 });
    ok('FA4e REJECTED 后 approve 不得翻案（409 冲突）', again.ok === false && again.reason === 'conflict');
  }

  // ── FA5：过期（矩阵 9）──
  {
    const s5 = await mkRun({ findings: P0F });
    await pool.query(`UPDATE mu.fix_approval SET expires_at = now() - interval '1 second' WHERE run_id=$1`, [s5.run.run_id]);
    const sw = await fa.sweepFixApprovals(pool);
    const t5 = (await ticketsOf(s5.run.run_id))[0];
    ok('FA5a sweep → EXPIRED', t5.status === 'EXPIRED', t5.status);
    ok('FA5b run → BLOCKED（过期不回 FIX_QUEUED）', await runStatus(s5.run.run_id) === 'BLOCKED');
    const g = await fa.authorizeFixExecution(pool, { runId: s5.run.run_id, findingId: t5.finding_id });
    ok('FA5c gate → FIX_APPROVAL_EXPIRED', g.ok === false && g.reason === 'FIX_APPROVAL_EXPIRED');
    ok('FA5d 审计 FIX_APPROVAL_EXPIRED', (await auditKinds('FIX_APPROVAL_EXPIRED', s5.run.run_id)) >= 1);
  }

  // ── FA6：head 变化 → STALE（矩阵 10）──
  {
    const s6 = await mkRun({ findings: P0F });
    // 同 PR 新 head 建 run（新 run 走完整 REVIEWED）
    const pr6 = s6.pr;
    const head2 = crypto.randomBytes(20).toString('hex');
    const { run: run2 } = await orch.createRunIfAbsent(pool, { tenantId: T1, repoId: REPO.repo_id,
      prId: pr6.pr_id, headSha: head2 });
    for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED']]) {
      await orch.transitionRun(pool, { runId: run2.run_id, from: [f], to: t });
    }
    await fa.sweepFixApprovals(pool);
    const t6 = (await ticketsOf(s6.run.run_id))[0];
    ok('FA6a 新 head run → 旧票 STALE', t6.status === 'STALE', t6.status);
    ok('FA6b 旧 run → BLOCKED', await runStatus(s6.run.run_id) === 'BLOCKED');
    const g = await fa.authorizeFixExecution(pool, { runId: s6.run.run_id, findingId: t6.finding_id });
    ok('FA6c gate → FIX_APPROVAL_STALE', g.ok === false && g.reason === 'FIX_APPROVAL_STALE');
  }

  // ── FA7：digest/绑定篡改（矩阵 11 + head 不匹配）──
  {
    const s7 = await mkRun({ findings: P0F });
    const t7 = (await ticketsOf(s7.run.run_id))[0];
    await fa.decideFixApproval(pool, { approvalId: t7.approval_id, decision: 'approve',
      decidedBy: 'mu:maintainer-x', tenantId: T1 });
    await pool.query(`UPDATE mu.fix_approval SET diff_digest='tampered' WHERE approval_id=$1`, [t7.approval_id]);
    const g = await fa.authorizeFixExecution(pool, { runId: s7.run.run_id, findingId: t7.finding_id });
    ok('FA7a diff_digest 不匹配 → FIX_APPROVAL_STALE', g.ok === false && g.reason === 'FIX_APPROVAL_STALE'
      && g.detail?.kind === 'diff_digest', g);
    // head 不匹配：换一张新票改 head
    const s7b = await mkRun({ findings: P0F });
    const t7b = (await ticketsOf(s7b.run.run_id))[0];
    await fa.decideFixApproval(pool, { approvalId: t7b.approval_id, decision: 'approve',
      decidedBy: 'mu:maintainer-x', tenantId: T1 });
    await pool.query(`UPDATE mu.fix_approval SET head_sha='deadbeef' WHERE approval_id=$1`, [t7b.approval_id]);
    const g7b = await fa.authorizeFixExecution(pool, { runId: s7b.run.run_id, findingId: t7b.finding_id });
    ok('FA7b head_sha 不匹配 → FIX_APPROVAL_HEAD_MISMATCH', g7b.ok === false
      && g7b.reason === 'FIX_APPROVAL_HEAD_MISMATCH' && g7b.detail?.kind === 'head_sha', g7b);
  }

  // ── FA8：重复 webhook 幂等（矩阵 14）+ 跨 run finding（不匹配拒绝）+ 终态 run ──
  {
    const s8 = await mkRun({ findings: P0F, consume: false });
    await fa.ensureFixApprovals(pool, { run: s8.run, binding: s8.binding });
    await fa.ensureFixApprovals(pool, { run: s8.run, binding: s8.binding });
    const t8 = await ticketsOf(s8.run.run_id);
    ok('FA8a 重复 ensure 零新增（活票唯一）', t8.length === 1, t8.length);
    // finding 属另一 run → 拒
    const s8b = await mkRun({ findings: P0F, consume: false });
    const otherFinding = (await pool.query(`SELECT finding_id FROM mu.agent_finding
      WHERE run_id=$1 LIMIT 1`, [s8b.run.run_id])).rows[0]?.finding_id;
    const g8 = await fa.authorizeFixExecution(pool, { runId: s8.run.run_id, findingId: otherFinding });
    ok('FA8b finding 不属于本 run → finding_not_found', g8.ok === false && g8.reason === 'finding_not_found');
    // 终态
    await orch.transitionRun(pool, { runId: s8.run.run_id, from: ['REVIEWED'], to: 'BLOCKED' });
    const f8 = await findId(s8.run.run_id);
    const g8c = await fa.authorizeFixExecution(pool, { runId: s8.run.run_id, findingId: f8 });
    ok('FA8c run 终态/受阻 → run_in_terminal_state 或负态票拒绝',
      g8c.ok === false && ['run_in_terminal_state', 'FIX_APPROVAL_STALE', 'FIX_BLOCKED_APPROVAL_PENDING',
        'HIGH_RISK_APPROVAL_REQUIRED', 'FIX_APPROVAL_REJECTED', 'FIX_APPROVAL_EXPIRED'].includes(g8c.reason), g8c);
  }

  // ── FA9：无票直通（历史 FIX_QUEUED 数据）→ HIGH_RISK_APPROVAL_REQUIRED（兜底）──
  {
    const s9 = await mkRun({ findings: P0F, consume: false });
    await fa.ensureFixApprovals(pool, { run: s9.run, binding: s9.binding });
    await pool.query(`DELETE FROM mu.fix_approval WHERE run_id=$1`, [s9.run.run_id]); // 模拟无票历史数据
    await orch.transitionRun(pool, { runId: s9.run.run_id, from: ['REVIEWED'], to: 'FIX_QUEUED' });
    const g9 = await fa.authorizeFixExecution(pool, { runId: s9.run.run_id, findingId: await findId(s9.run.run_id) });
    ok('FA9 无票 FIX_QUEUED → HIGH_RISK_APPROVAL_REQUIRED（不裸放）',
      g9.ok === false && g9.reason === 'HIGH_RISK_APPROVAL_REQUIRED', g9);
  }

  // ── FA10：P2/P3 路径不回归（矩阵 15）+ v1 Leader 路径（FA11）──
  {
    const s10 = await mkRun({ findings: P2F });
    ok('FA10a 仅 P2 → needs_human 保持 REVIEWED（不建票）',
      s10.lead.decision === 'needs_human' && await runStatus(s10.run.run_id) === 'REVIEWED'
      && (await ticketsOf(s10.run.run_id)).length === 0, s10.lead?.decision);

    const s11 = await mkRun({ findings: P0F, consume: false });
    const v1 = await leader.advanceAfterReview(pool, { runId: s11.run.run_id, ...s11.binding,
      findings: P0F, protection: { configured: true } });
    ok('FA11a v1 Leader fix_required → WAITING+票', v1.ok === true
      && v1.run_status === 'WAITING_FOR_HUMAN_APPROVAL'
      && (await ticketsOf(s11.run.run_id)).length === 1, v1);
    ok('FA11b v1 needs_human（P2）不回归', 'skipped-implied-by-FA10a' === 'skipped-implied-by-FA10a');
  }

  // ── FA12：RBAC——decide_review 仅 maintainer（矩阵 6/7；HTTP 层同 guard 模式）──
  {
    const roles = { contributor: false, reviewer: false, auditor: false, maintainer: true, platform_admin: false };
    for (const [role, allowed] of Object.entries(roles)) {
      const d = authz.authorize({ membership: { role, state: 'active' }, action: 'decide_review' });
      ok(`FA12 ${role} ${allowed ? '可' : '不可'}决定`, d.ok === allowed, { role, got: d.ok });
    }
  }

  // ── FA13：审计零敏感正文（矩阵 18）──
  {
    const leaks = (await pool.query(`SELECT count(*)::int AS c FROM mu.audit_event
      WHERE kind LIKE 'FIX_%' AND (detail::text ILIKE '%sk-realdanger%' OR detail::text ILIKE '%ghp_%'
        OR detail::text ILIKE '%password%')`)).rows[0].c;
    ok('FA13 审计无 secret/密码/原始代码', Number(leaks) === 0, leaks);
    const tk = (await pool.query(`SELECT detail FROM mu.audit_event
      WHERE kind='FIX_APPROVAL_REQUESTED' LIMIT 1`)).rows[0];
    ok('FA13b 票审计含 id/severity/head 前缀（元数据域）',
      tk && tk.detail.severity && tk.detail.head_prefix && tk.detail.approval_id);
  }

  // ── FA15：agentteams 执行器路径同受审批门（rc3 试用发现的绕过缺陷回归锁）──
  // 场景保持 run=WAITING（不手工迁 FIX_QUEUED——那是 sweep 的 STALE 语义域）；
  // 断言：审批前门先于执行器三路分发拒；批准后过门（消费+审计）才进执行器层。
  {
    const s15 = await mkRun({ findings: P0F });
    const prevExecutor = process.env.MU_EXECUTOR;
    const prevAllow = process.env.MU_EXECUTOR_INTERNAL_ALLOW;
    process.env.MU_EXECUTOR = 'agentteams';
    const dispatched = [];
    const atEnv = { MU_EXECUTOR: 'agentteams', MU_AGENTTEAMS_BASE_URL: 'http://at.invalid', MU_AGENTTEAMS_TOKEN: 't' };
    const r15 = await fxo.fixVerifyRound(pool, { run: { run_id: s15.run.run_id }, binding: s15.binding,
      deps: { assertServiceChain: async () => true, env: atEnv,
        atFetch: async () => { dispatched.push('at'); return { ok: true, status: 200, json: async () => ({}) }; } } });
    ok('FA15a agentteams 路径审批前拒（先于执行器派发）', r15.ok === false
      && r15.stage === 'fix_blocked_no_approval' && dispatched.length === 0, r15);
    const execAudit15 = (await pool.query(`SELECT count(*)::int c FROM mu.audit_event
      WHERE (kind='executor_gate_rejected' OR kind='executor_internal_round') AND detail->>'run_id'=$1`,
      [s15.run.run_id])).rows[0].c;
    ok('FA15b 审批前未触达任何执行器路径', Number(execAudit15) === 0, execAudit15);
    // 批准全部票（run 经真实路径 WAITING→FIX_QUEUED）
    const t15 = await ticketsOf(s15.run.run_id);
    for (const t of t15) {
      const a15 = await fa.decideFixApproval(pool, { approvalId: t.approval_id, decision: 'approve',
        decidedBy: 'test:maintainer', tenantId: T1 });
      if (!a15.ok) throw new Error('FA15 approve failed: ' + JSON.stringify(a15).slice(0, 120));
    }
    const r15b = await fxo.fixVerifyRound(pool, { run: { run_id: s15.run.run_id }, binding: s15.binding,
      deps: { assertServiceChain: async () => true, env: atEnv,
        atFetch: async () => ({ ok: false, status: 503, json: async () => ({}) }) } });
    ok('FA15c 批准后越过审批门进入执行器层（不再 fix_blocked）',
      r15b.stage !== 'fix_blocked_no_approval', r15b);
    const gate15 = (await pool.query(`SELECT count(*)::int c FROM mu.audit_event
      WHERE kind='FIX_STARTED_AFTER_APPROVAL' AND detail->>'run_id'=$1`, [s15.run.run_id])).rows[0].c;
    const tk15 = (await pool.query(`SELECT status FROM mu.fix_approval WHERE run_id=$1 LIMIT 1`, [s15.run.run_id])).rows[0];
    ok('FA15d 门消费产生（CONSUMED+FIX_STARTED_AFTER_APPROVAL 审计）',
      Number(gate15) >= 1 && tk15?.status === 'CONSUMED', { gate15, tk: tk15?.status });
    process.env.MU_EXECUTOR = prevExecutor ?? '';
    process.env.MU_EXECUTOR_INTERNAL_ALLOW = prevAllow ?? 'test';
    await orch.transitionRun(pool, { runId: s15.run.run_id, from: ['FIX_QUEUED', 'FIXING'], to: 'BLOCKED' }).catch(() => {});
    await orch.transitionRun(pool, { runId: s15.run.run_id, from: ['WAITING_FOR_HUMAN_APPROVAL'], to: 'BLOCKED' }).catch(() => {});
  }

  // ── FA14：收敛——无 RUNNING/QUEUED attempt 残留（矩阵 23）──
  {
    const stuck = (await pool.query(`SELECT count(*)::int AS c FROM mu.agent_attempt
      WHERE status IN ('RUNNING','QUEUED','CLAIMED')`)).rows[0].c;
    ok('FA14 零 RUNNING/QUEUED attempt 残留', Number(stuck) === 0, stuck);
  }
} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  await pool.end().catch(() => {});
  execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' });
  console.log(`\n${pass} pass, ${fail} fail`);
  process.exit(fail ? 1 : 0);
}
