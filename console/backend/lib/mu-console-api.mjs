// console/backend/lib/mu-console-api.mjs — MU 模式下旧 Console 页面的数据适配层。
//
// 职责：从 MU canonical 表读取数据，输出与旧 legacy 端点相同的数据形状，
// 使旧前端页面无需改动即可在 MU 模式下工作。
//
// 数据来源（全部带 tenant_id 隔离）：
//   mu.repository + mu.pull_request + mu.review_run
//   mu.agent_attempt + mu.agent_finding + mu.fix_attempt + mu.verification_attempt
//   mu.dead_letter + mu.audit_event + mu.repository_binding

export function createMuConsoleApi({ pool }) {
  if (!pool) throw new Error('pool required');

  async function q(text, params) {
    const r = await pool.query(text, params);
    return r.rows;
  }

  // ── Overview ──
  // MU 状态 → legacy stage 映射（OverviewPage 使用 legacy stage key 空间）
  const MU_TO_STAGE = {
    'RECEIVED': 'PENDING',
    'REVIEW_QUEUED': 'PENDING',
    'REVIEWING': 'REVIEWING',
    'REVIEWED': 'REVIEWING',
    'FIX_QUEUED': 'REMEDIATING',
    'WAITING_FOR_HUMAN_APPROVAL': 'ACTION_REQUIRED', // v16 高危审批门
    'VERIFY_QUEUED': 'VERIFYING',
    'VERIFYING': 'VERIFYING',
    'VERIFIED': 'PASSED',
    'REWORK_REQUIRED': 'ACTION_REQUIRED',
    'BLOCKED': 'BLOCKED',
    'COMPLETED': 'PASSED',
  };
  const STAGE_KEYS = ['REVIEWING', 'ACTION_REQUIRED', 'REMEDIATING', 'VERIFYING', 'PASSED', 'BLOCKED', 'STALE', 'UNKNOWN', 'PENDING'];

  async function overview(tenantId) {
    const repoCounts = await q(
      `SELECT r.owner || '/' || r.name AS repo,
              count(DISTINCT pr.pr_id) AS prs,
              count(DISTINCT rr.run_id) AS runs,
              count(DISTINCT CASE WHEN rr.status IN ('FIX_QUEUED','VERIFY_QUEUED','REWORK_REQUIRED','WAITING_FOR_HUMAN_APPROVAL') THEN rr.run_id END) AS pending
       FROM mu.repository r
       LEFT JOIN mu.pull_request pr ON pr.repo_id = r.repo_id AND pr.tenant_id = r.tenant_id
       LEFT JOIN mu.review_run rr ON rr.repo_id = r.repo_id AND rr.tenant_id = r.tenant_id
       WHERE r.tenant_id=$1 AND r.state='active'
       GROUP BY r.owner, r.name, r.created_at ORDER BY r.created_at DESC`, [tenantId]);

    const prs = await q(
      `SELECT pr.pr_id, pr.provider_pr_number, pr.title, pr.state, pr.head_sha,
              r.owner, r.name AS repo_name,
              COALESCE(latest_rr.status, 'PENDING') AS stage,
              'mu_review_run' AS stage_source,
              latest_rr.run_id, latest_rr.created_at AS latest
       FROM mu.pull_request pr
       JOIN mu.repository r ON r.repo_id = pr.repo_id
       LEFT JOIN LATERAL (
         SELECT run_id, status, created_at FROM mu.review_run
          WHERE tenant_id=$1 AND repo_id=pr.repo_id AND pr_id=pr.pr_id
          ORDER BY created_at DESC LIMIT 1
       ) latest_rr ON true
       WHERE pr.tenant_id=$1
       ORDER BY pr.updated_at DESC LIMIT 50`, [tenantId]);

    // stage_counts 映射到 legacy stage key 空间
    const stageRows = await q(
      `SELECT status, count(*) AS c FROM mu.review_run WHERE tenant_id=$1 GROUP BY status`, [tenantId]);
    const stage_counts = Object.fromEntries(STAGE_KEYS.map(s2 => [s2, 0]));
    for (const row of stageRows) {
      const mapped = MU_TO_STAGE[row.status] ?? 'UNKNOWN';
      stage_counts[mapped] = (stage_counts[mapped] ?? 0) + Number(row.c);
    }

    const findings = await q(
      `SELECT f.rule_id, f.severity, f.path, f.summary_masked, f.created_at,
              r.owner, r.name AS repo_name
       FROM mu.agent_finding f
       JOIN mu.review_run rr ON rr.run_id = f.run_id
       JOIN mu.repository r ON r.repo_id = rr.repo_id
       WHERE rr.tenant_id=$1
       ORDER BY f.created_at DESC LIMIT 20`, [tenantId]);

    const blocked = await q(
      `SELECT run_id, status, updated_at FROM mu.review_run
       WHERE tenant_id=$1 AND status IN ('BLOCKED','REWORK_REQUIRED')
       ORDER BY updated_at DESC LIMIT 10`, [tenantId]);

    // pending summary（待处理数量）
    const pendingRows = await q(
      `SELECT count(*) AS c FROM mu.review_run
       WHERE tenant_id=$1 AND status IN ('FIX_QUEUED','VERIFY_QUEUED','REWORK_REQUIRED','BLOCKED','WAITING_FOR_HUMAN_APPROVAL')`, [tenantId]);
    const pendingCount = Number(pendingRows[0]?.c ?? 0);

    return {
      prs: prs.map(p => ({
        repo: p.owner + '/' + p.repo_name,
        pr: p.provider_pr_number,
        head_sha: p.head_sha,
        run_id: p.run_id,
        latest: p.latest,
        stage: MU_TO_STAGE[p.stage] ?? p.stage,
        stage_source: p.stage_source,
      })),
      tickets: [],
      evidence: findings.map(f => ({
        rule_id: f.rule_id, severity: f.severity, path: f.path,
        summary: f.summary_masked, repo: f.owner + '/' + f.repo_name,
      })),
      gate_decisions: blocked.map(b => ({
        run_id: b.run_id, decision: { stage: b.status },
        created_at: b.updated_at,
      })),
      repository_counts: repoCounts.map(r => ({
        repo: r.repo, prs: Number(r.prs), runs: Number(r.runs), pending: Number(r.pending),
      })),
      stage_counts,
      pending_summary: { count: pendingCount, oldest_pending_at: null, oldest_wait_minutes: null },
      incidents: { stale_count: 0, failed_receipts: 0, integrity_conflicts: 0 },
      health: {
        postgres: 'LIVE',
        minio: { state: 'AGENTTEAMS_MANAGED', note: 'MinIO 由 AgentTeams 内部管理（worker 配置/任务工件）——控制台按安全边界不直连，属正常' },
        backend: { state: 'OK', note: '本服务即后端（只读）' },
      },
      trend: [],
      schema_version: 1,
      generated_at: new Date().toISOString(),
      stages_enum: ['REVIEWING', 'ACTION_REQUIRED', 'REMEDIATING', 'VERIFYING', 'PASSED', 'BLOCKED', 'STALE', 'UNKNOWN', 'PENDING'],
      mode: 'mu_canonical',
      source: 'MU_CANONICAL_LIVE',
      total_repos: repoCounts.length,
      total_prs: prs.length,
      total_findings: findings.length,
      total_blocked: blocked.length,
    };
  }

  // ── Pulls (PR list) ──
  async function pulls(tenantId) {
    const rows = await q(
      `SELECT pr.pr_id, pr.provider_pr_number, pr.title, pr.state, pr.head_sha,
              r.owner, r.name AS repo_name,
              rr.status AS run_status, rr.run_id
       FROM mu.pull_request pr
       JOIN mu.repository r ON r.repo_id = pr.repo_id
       LEFT JOIN LATERAL (
         SELECT run_id, status FROM mu.review_run
          WHERE tenant_id=$1 AND repo_id=pr.repo_id AND pr_id=pr.pr_id
          ORDER BY created_at DESC LIMIT 1
       ) rr ON true
       WHERE pr.tenant_id=$1 AND pr.state='open'
       ORDER BY pr.updated_at DESC LIMIT 50`, [tenantId]);
    return rows.map(r => ({
      repo: r.owner + '/' + r.repo_name,
      pr: r.provider_pr_number,
      title: r.title,
      head_sha: r.head_sha?.slice(0, 12),
      // run_id：SELECT 已取但此前投影丢弃——CorePage「PR / Head」表 Run 列需要（诚实展示最近 run）
      run_id: r.run_id ?? null,
      status: r.run_status ?? 'no_review',
      stage: r.run_status ?? 'PENDING',
    }));
  }

  // ── Pending (需要处理的事项) ──
  async function pending(tenantId) {
    const rows = await q(
      `SELECT rr.run_id, rr.status, rr.updated_at, r.owner, r.name AS repo_name,
              pr.provider_pr_number, pr.title
       FROM mu.review_run rr
       JOIN mu.repository r ON r.repo_id = rr.repo_id
       JOIN mu.pull_request pr ON pr.pr_id = rr.pr_id
       WHERE rr.tenant_id=$1 AND rr.status IN ('FIX_QUEUED','VERIFY_QUEUED','REWORK_REQUIRED','BLOCKED','WAITING_FOR_HUMAN_APPROVAL')
       ORDER BY rr.updated_at DESC LIMIT 20`, [tenantId]);
    return rows.map(r => ({
      run_id: r.run_id, status: r.status,
      repo: r.owner + '/' + r.repo_name, pr: r.provider_pr_number, title: r.title,
      updated_at: r.updated_at,
    }));
  }

  // ── Runs (运行历史) ──
  async function runs(tenantId, limit = 50) {
    const rows = await q(
      `SELECT rr.run_id, rr.status, rr.head_sha, rr.created_at, rr.updated_at,
              r.owner, r.name AS repo_name, pr.provider_pr_number
       FROM mu.review_run rr
       JOIN mu.repository r ON r.repo_id = rr.repo_id
       LEFT JOIN mu.pull_request pr ON pr.pr_id = rr.pr_id
       WHERE rr.tenant_id=$1
       ORDER BY rr.created_at DESC LIMIT $2`, [tenantId, limit]);
    return rows.map(r => ({
      run_id: r.run_id, status: r.status, head_sha: r.head_sha?.slice(0, 12),
      repo: r.owner + '/' + r.repo_name, pr: r.provider_pr_number,
      created_at: r.created_at, updated_at: r.updated_at,
    }));
  }

  // ── Run 详情 ──
  async function runDetail(tenantId, runId) {
    const rows = await q(
      `SELECT rr.*, r.owner, r.name AS repo_name FROM mu.review_run rr
       JOIN mu.repository r ON r.repo_id = rr.repo_id
       WHERE rr.tenant_id=$1 AND rr.run_id=$2`, [tenantId, runId]);
    if (!rows.length) return null;
    const run = rows[0];
    const attempts = await q(
      `SELECT attempt_id, agent_role, provider, status, created_at FROM mu.agent_attempt
       WHERE run_id=$1 ORDER BY created_at`, [runId]);
    const findings = await q(
      `SELECT rule_id, severity, path, line_start, title, summary_masked FROM mu.agent_finding
       WHERE run_id=$1 ORDER BY severity`, [runId]);
    const fixes = await q(
      `SELECT fix_id, status, patch_digest, error_code FROM mu.fix_attempt
       WHERE run_id=$1 ORDER BY created_at`, [runId]);
    const verifications = await q(
      `SELECT verdict, error_code FROM mu.verification_attempt
       WHERE run_id=$1 ORDER BY created_at`, [runId]);
    const decisions = await q(
      `SELECT stage, decision, created_at FROM mu.orchestration_decision
       WHERE run_id=$1 ORDER BY created_at`, [runId]);
    return { run, attempts, findings, fixes, verifications, decisions };
  }

  // ── 仓库列表 ──
  async function repositories(tenantId) {
    const rows = await q(
      `SELECT r.repo_id, r.owner, r.name, r.state, r.created_at,
              rb.binding_state,
              (SELECT count(*) FROM mu.pull_request p WHERE p.repo_id=r.repo_id) AS pr_count
       FROM mu.repository r
       LEFT JOIN mu.repository_binding rb ON rb.repo_id = r.repo_id
       WHERE r.tenant_id=$1 ORDER BY r.created_at DESC`, [tenantId]);
    return rows;
  }

  // ── 审计 ──
  async function audit(tenantId, limit = 50) {
    return q(
      `SELECT kind, detail, created_at FROM mu.audit_event
       WHERE tenant_id=$1 ORDER BY created_at DESC LIMIT $2`, [tenantId, limit]);
  }

  // ── AgentTeams 状态 ──
  async function agentteamsStatus(tenantId) {
    const attempts = await q(
      `SELECT provider, status, count(*) c FROM mu.agent_attempt
       WHERE run_id IN (SELECT run_id FROM mu.review_run WHERE tenant_id=$1)
         AND provider='agentteams'
       GROUP BY provider, status`, [tenantId]);
    const done = attempts.filter(a => a.status === 'DONE').reduce((s, a) => s + Number(a.c), 0);
    const failed = attempts.filter(a => a.status === 'FAILED').reduce((s, a) => s + Number(a.c), 0);
    return { attempts_done: done, attempts_failed: failed };
  }

  // ── PR 详情（contract 形状——/api/pulls/:n 的 MU facade 后端）──
  // 全程 tenant_id 收窄：repo 按 owner/name 解析（tenant 内唯一），PR 按 repo+编号。
  // verdict 由 run 状态映射（不伪造 GitHub review）；stale=run head ≠ PR 当前 head。
  const VERDICT_BY_RUN = {
    COMPLETED: 'PASS', VERIFIED: 'PASS',
    BLOCKED: 'BLOCKED', REWORK_REQUIRED: 'BLOCKED', FAILED: 'BLOCKED',
    RECEIVED: 'RUNNING', REVIEW_QUEUED: 'RUNNING', REVIEWING: 'RUNNING',
    REVIEWED: 'RUNNING', WAITING_FOR_HUMAN_APPROVAL: 'RUNNING', FIX_QUEUED: 'RUNNING', FIXING: 'RUNNING',
    VERIFY_QUEUED: 'RUNNING', VERIFYING: 'RUNNING',
  };
  async function pullDetail(tenantId, repoFullName, prNumber) {
    if (!repoFullName || !prNumber) return null;
    const repoRows = await q(
      `SELECT repo_id, owner, name FROM mu.repository
        WHERE tenant_id=$1 AND state='active' AND owner || '/' || name = $2 LIMIT 1`,
      [tenantId, String(repoFullName)]);
    if (!repoRows.length) return null;
    const repo = repoRows[0];
    const prRows = await q(
      `SELECT pr_id, provider_pr_number, title, state, head_sha, updated_at FROM mu.pull_request
        WHERE tenant_id=$1 AND repo_id=$2 AND provider_pr_number=$3
        ORDER BY updated_at DESC LIMIT 1`, [tenantId, repo.repo_id, Number(prNumber)]);
    if (!prRows.length) return null;
    const pr = prRows[0];
    const runRows = await q(
      `SELECT run_id, status, head_sha, trigger_source, created_at, updated_at FROM mu.review_run
        WHERE tenant_id=$1 AND repo_id=$2 AND pr_id=$3
        ORDER BY created_at DESC LIMIT 50`, [tenantId, repo.repo_id, pr.pr_id]);
    const cur = runRows[0] ?? null;
    const findings = cur ? await q(
      `SELECT severity FROM mu.agent_finding WHERE run_id=$1 ORDER BY severity LIMIT 500`, [cur.run_id]) : [];
    const sevRank = { P0: 0, P1: 1, P2: 2, P3: 3 };
    const topSeverity = findings.length
      ? findings.map((f) => f.severity).sort((a, b) => (sevRank[a] ?? 9) - (sevRank[b] ?? 9))[0]
      : null;
    return {
      repo: repo.owner + '/' + repo.name,
      pr_number: pr.provider_pr_number,
      title: pr.title ?? null,
      // GitHub 当前 head 权威以 mu.pull_request 快照为准（webhook 同步），不以审查 head 冒充
      current_head_sha: pr.head_sha ?? null,
      state: pr.state ?? null,
      stage: cur ? (MU_TO_STAGE[cur.status] ?? 'UNKNOWN') : 'PENDING',
      stage_source: cur ? 'mu_review_run' : 'mu_pull_request',
      head_sha: cur?.head_sha ?? null,
      run_id: cur?.run_id ?? null,
      updated_at: cur?.updated_at ?? pr.updated_at,
      latest_result: cur ? {
        run_id: cur.run_id, head_sha: cur.head_sha,
        verdict: VERDICT_BY_RUN[cur.status] ?? 'RUNNING',
        severity: topSeverity,
        stale: pr.head_sha ? cur.head_sha !== pr.head_sha : false,
        published: false, // AI 审查不构成 GitHub required review——绝不宣称已发布
      } : null,
      latest_run: cur ? { run_id: cur.run_id, status: cur.status, started_at: cur.created_at } : null,
      runs: runRows.map((r) => ({
        run_id: r.run_id, created_at: r.created_at, status: r.status,
        stage: MU_TO_STAGE[r.status] ?? 'UNKNOWN', stage_source: 'mu_review_run',
        outcome: r.status, head_sha: r.head_sha,
        stale: pr.head_sha ? r.head_sha !== pr.head_sha : false,
        trigger: r.trigger_source === 'manual' ? 'manual' : 'github_event',
      })),
      receipts: { total: 0, ok: 0, integrity_conflicts: 0 },
      gate_audit: [],
      has_pending_tickets: false,
      merge_panel: { enabled: false, reasons: ['merge_disabled'],
        github_url: `https://github.com/${repo.owner}/${repo.name}/pull/${pr.provider_pr_number}` },
      data_source: 'MU_CANONICAL_LIVE',
      tenant_scope: 'self',
      as_of: new Date().toISOString(),
      schema_version: 1,
    };
  }

  return { overview, pulls, pending, runs, runDetail, pullDetail, repositories, audit, agentteamsStatus };
}
