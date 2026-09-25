// console/backend/lib/core-pilot.mjs — 核心控制面五 API 的 PG 实时状态读取
// （CANONICAL_CONSOLE_PROMOTION；迁移自 demo-platform 已验证实现）。
// 诚实语义：无 DSN → BACKEND_NOT_WIRED；连接/查询失败 → BACKEND_ERROR（带错误码，
// 永不假成功）；成功 → POSTGRESQL_LIVE。JSONB payload 抽取、顺序查询（单 client
// 不支持并发）、仅缓存 LIVE 结果（错误态每次重探）。Node pg 为可选依赖：
// 未安装时按 NOT_WIRED 处理（不伪造）。
const CACHE_TTL_MS = 5000;
let cache = { data: null, at: 0 };

// R4（FB-05）：时间值 → UTC 日期桶（YYYY-MM-DD）。字符串取 ISO 前 10 位；
// Date（pg 驱动对 timestamptz 的返回）走 toISOString。非法/缺失 → null。
export function isoDayOf(v) {
  if (v == null || v === '') return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v.toISOString().slice(0, 10);
  if (typeof v === 'string') return /^\d{4}-\d{2}-\d{2}/.test(v) ? v.slice(0, 10) : null;
  const d = new Date(v);
  return isNaN(d.getTime()) ? null : d.toISOString().slice(0, 10);
}

function parseDsn(s) {
  const cfg = {};
  for (const kv of s.trim().split(/\s+/)) {
    const [k, ...v] = kv.split('=');
    if (!k || v.length === 0) continue;
    cfg[k] = v.join('=');
  }
  return {
    host: cfg.host || '127.0.0.1',
    port: parseInt(cfg.port || '5432', 10),
    user: cfg.user || 'postgres',
    password: cfg.password || '',
    database: (cfg.dbname || cfg.database || '').replace(/^"|"$/g, ''),
  };
}

export async function corePilotState() {
  const dsn = process.env.CONSOLE_PG_DSN;
  if (!dsn) {
    return { source: 'BACKEND_NOT_WIRED', pulls: [], pending: [], tickets: [],
             evidence: [], gate_decisions: [] };
  }
  if (cache.data && Date.now() - cache.at < CACHE_TTL_MS) return cache.data;
  let client;
  try {
    const pg = await import('pg');
    const connOpts = dsn.startsWith('postgres') ? { connectionString: dsn } : parseDsn(dsn);
    client = new pg.Client({ ...connOpts, connectionTimeoutMillis: 3000 });
    await client.connect();
    // pg 不支持单 client 并发查询；JSONB 字段需 ->>' 抽取
    const pulls = await client.query(
      "SELECT o.payload->>'repo' AS repo, " +
      "COALESCE((o.payload->>'pr')::int, " +
      "  (SELECT min(t.pr_number) FROM approval.tickets t WHERE t.run_id = o.payload->>'run_id'), " +
      "  (SELECT max((a.decision->>'pr')::int) FROM skill_gate_audit a " +
      "   WHERE a.run_id = o.payload->>'run_id' AND a.decision ? 'pr')) AS pr_number, " +
      "o.payload->>'head_sha' AS head_sha, o.payload->>'run_id' AS run_id, " +
      "MAX(o.created_at) AS latest " +
      "FROM skill_receipt_outbox o GROUP BY 1,2,3,4 ORDER BY latest DESC LIMIT 50");
    const tickets = await client.query(
      "SELECT ticket_id, run_id, repo_id, pr_number, head_sha, action, status, " +
      "finding_id, finding_fingerprint, target_key, created_at, approval_expires_at " +
      "FROM approval.tickets ORDER BY created_at DESC LIMIT 50");
    const receipts = await client.query(
      "SELECT skill_name, status, payload->>'binding_status' AS binding_status, " +
      "payload->>'integrity' AS integrity, payload->>'run_id' AS run_id, " +
      "payload->>'repo' AS repo, payload->>'head_sha' AS head_sha, attempt, " +
      "(payload->>'duration_ms') AS duration_ms, created_at " +
      "FROM skill_receipt_outbox ORDER BY created_at DESC LIMIT 50");
    let audit = { rows: [] };
    try {
      audit = await client.query('SELECT run_id, decision, created_at FROM skill_gate_audit ORDER BY created_at DESC LIMIT 50');
    } catch { /* 表可能不存在于部分部署 */
    }
    const pending = tickets.rows.filter(t => t.status === 'PENDING').map(t => ({
      ticket_id: t.ticket_id, run_id: t.run_id, repo: t.repo_id, pr_number: t.pr_number,
      head_sha: t.head_sha, action: t.action, status: t.status,
      finding_id: t.finding_id, target_key: t.target_key,
      created_at: t.created_at, approval_expires_at: t.approval_expires_at,
    }));
    const data = {
      source: 'POSTGRESQL_LIVE',
      pulls: pulls.rows.map(p => ({ repo: p.repo, pr_number: p.pr_number,
        head_sha: p.head_sha, run_id: p.run_id })),
      tickets: tickets.rows.map(t => ({
        ticket_id: t.ticket_id, run_id: t.run_id, repo: t.repo_id, pr_number: t.pr_number,
        head_sha: t.head_sha, action: t.action, status: t.status,
        finding_id: t.finding_id, finding_fingerprint: t.finding_fingerprint,
        target_key: t.target_key, created_at: t.created_at,
        approval_expires_at: t.approval_expires_at,
        is_expired: t.approval_expires_at ? new Date(t.approval_expires_at) < new Date() : false,
      })),
      pending,
      evidence: receipts.rows.map(r => ({
        skill_name: r.skill_name, status: r.status, binding_status: r.binding_status,
        integrity: r.integrity, run_id: r.run_id, repo: r.repo, head_sha: r.head_sha,
        attempt: r.attempt, duration_ms: r.duration_ms, created_at: r.created_at,
      })),
      gate_decisions: audit.rows.map(a => ({
        run_id: a.run_id, decision: a.decision, created_at: a.created_at,
      })),
    };
    cache = { data, at: Date.now() };   // 仅缓存 LIVE；错误/未接线每次重探
    return data;
  } catch (err) {
    const detail = (err && (err.code || (err.message ? err.message.slice(0, 80) : null))) || String(err).slice(0, 80);
    return { source: 'BACKEND_ERROR', error: detail, pulls: [], pending: [],
             tickets: [], evidence: [], gate_decisions: [] };
  } finally {
    if (client) client.end().catch(() => {});
  }
}

export function clearCorePilotCache() { cache = { data: null, at: 0 }; }

// ── 运营总览（CANONICAL_CONSOLE_OPERATIONAL_OVERVIEW）───────────────────
// GET /api/overview 的数据推导。阶段只能由后端权威状态（PG 事实）给出：
//   ACTION_REQUIRED = 最新 run 存在未过期 PENDING 票据（approval.tickets）
//   BLOCKED         = gate 审计 REFUSE（skill_gate_audit）或回执 integrity != OK
//   PASSED          = 回执齐备 + gate PRODUCE + 无待办票
//   REVIEWING       = 有回执、尚无 gate 决策
//   STALE           = 同 PR 存在更新 head 而该 run 绑定旧 head（head 排序推导）
//   REMEDIATING/VERIFYING = 需要 Fixer/Verifier（本部署 DISABLED）——计数恒 0，
//                            枚举保留但不虚构（stage_source 注明 not_applicable）。
// 每行保留 repo/pr_number/head_sha/run_id/stage/stage_source/updated_at。
const OVERVIEW_SCHEMA_VERSION = 1;
const STAGES = ['REVIEWING', 'ACTION_REQUIRED', 'REMEDIATING', 'VERIFYING', 'PASSED', 'BLOCKED', 'STALE'];

export async function overviewState(allowRepos) {
  const base = await corePilotState();
  const out = {
    schema_version: OVERVIEW_SCHEMA_VERSION,
    generated_at: new Date().toISOString(),
    source: base.source,
    stages_enum: STAGES,
    ...(base.error ? { error: base.error } : {}),
    stage_counts: Object.fromEntries(STAGES.map((s) => [s, 0])),
    prs: [],
    repository_counts: [],
    trend: [],
    pending_summary: { count: 0, oldest_pending_at: null, oldest_wait_minutes: null },
    incidents: { stale_count: 0, failed_receipts: 0, integrity_conflicts: 0 },
    health: {
      postgres: base.source === 'POSTGRESQL_LIVE' ? 'LIVE'
        : base.source === 'BACKEND_ERROR' ? 'ERROR' : 'NOT_WIRED',
      minio: { state: 'NOT_WIRED', note: '控制台不直连 MinIO（平台边界）；证据链 MinIO 只读回读在验证工件中' },
      backend: { state: 'OK', note: '本服务即后端（只读）' },
    },
  };
  // 趋势骨架恒为近 14 天（未接线/错误时全零——诚实空，不虚构）
  const _today = new Date();
  for (let i = 13; i >= 0; i--) {
    out.trend.push({ date: new Date(_today.getTime() - i * 86400000).toISOString().slice(0, 10), runs: 0 });
  }
  if (base.source !== 'POSTGRESQL_LIVE') return out;   // 未接线/错误：诚实空聚合

  const allow = new Set(allowRepos);
  // receipts 按 run 聚合（allowlist 内）
  const byRun = new Map();
  for (const r of base.evidence) {
    if (r.repo && !allow.has(r.repo)) continue;
    if (!byRun.has(r.run_id)) {
      byRun.set(r.run_id, { run_id: r.run_id, repo: r.repo, head_sha: r.head_sha,
        skills: new Set(), ok: true, integrity_ok: true, latest_at: r.created_at });
    }
    const g = byRun.get(r.run_id);
    g.skills.add(r.skill_name);
    if (r.status !== 'OK') g.ok = false;
    if (r.integrity && r.integrity !== 'OK') g.integrity_ok = false;
    if (r.created_at && (!g.latest_at || r.created_at > g.latest_at)) g.latest_at = r.created_at;
  }
  // gate 决策（allowlist 内，decision.repo 已知行）
  const gates = new Map();
  for (const g of base.gate_decisions) {
    const repo = g.decision && g.decision.repo;
    if (repo && !allow.has(repo)) continue;
    if (!gates.has(g.run_id) || g.created_at > gates.get(g.run_id).created_at) gates.set(g.run_id, g);
  }
  // 票据（PENDING 未过期，allowlist 内）
  const pendByRun = new Map();
  const now = Date.now();
  let oldest = null;
  for (const t of base.tickets) {
    if (!allow.has(t.repo)) continue;
    if (t.status !== 'PENDING') continue;
    if (t.approval_expires_at && new Date(t.approval_expires_at) < new Date()) continue;
    if (!pendByRun.has(t.run_id) || t.created_at < pendByRun.get(t.run_id).created_at) {
      pendByRun.set(t.run_id, t);
    }
    if (!oldest || t.created_at < oldest) oldest = t.created_at;
  }
  out.pending_summary.count = pendByRun.size;
  if (oldest) {
    out.pending_summary.oldest_pending_at = oldest;
    out.pending_summary.oldest_wait_minutes = Math.max(0, Math.round((now - new Date(oldest)) / 60000));
  }
  // PR 分组：repo+pr（pr 来自 pulls 聚合的 COALESCE）
  const prRows = new Map();
  for (const p of base.pulls) {
    if (!allow.has(p.repo) || p.pr_number == null) continue;
    const key = `${p.repo}#${p.pr_number}`;
    if (!prRows.has(key)) prRows.set(key, { repo: p.repo, pr_number: p.pr_number, heads: [] });
    prRows.get(key).heads.push(p);
  }
  const incidents = out.incidents;
  for (const receipt of base.evidence) {
    if (receipt.repo && !allow.has(receipt.repo)) continue;
    if (receipt.status && receipt.status !== 'OK') incidents.failed_receipts += 1;
    if (receipt.integrity && receipt.integrity !== 'OK') incidents.integrity_conflicts += 1;
  }
  for (const [key, pr] of prRows) {
    // 每 head 的 runs
    const headRuns = pr.heads.map((h) => ({
      head_sha: h.head_sha, run_id: h.run_id,
      runs: [...byRun.values()].filter((g) => g.repo === pr.repo && g.head_sha === h.head_sha),
    })).sort((a, b) => String(b.heads_updated ?? '').localeCompare(String(a.heads_updated ?? '')));
    // 最新 head = receipts 时间最晚的 head
    let newest = null;
    for (const hr of headRuns) {
      for (const g of hr.runs) {
        if (!newest || (g.latest_at && (!newest.at || g.latest_at > newest.at))) {
          newest = { head_sha: hr.head_sha, at: g.latest_at };
        }
      }
    }
    for (const hr of headRuns) {
      for (const g of hr.runs) {
        let stage, stage_source;
        if (newest && hr.head_sha !== newest.head_sha) {
          stage = 'STALE'; stage_source = 'head-ordering (newer head exists)';
          incidents.stale_count += 1;
        } else if (pendByRun.has(g.run_id)) {
          stage = 'ACTION_REQUIRED'; stage_source = 'approval.tickets (PENDING)';
        } else if (!g.integrity_ok) {
          stage = 'BLOCKED'; stage_source = 'skill_receipt_outbox.integrity';
        } else {
          const gate = gates.get(g.run_id);
          if (!gate) { stage = 'REVIEWING'; stage_source = 'skill_receipt_outbox (no gate decision yet)'; }
          else if (String(gate.decision?.decision || '').toUpperCase() === 'REFUSE') {
            stage = 'BLOCKED'; stage_source = 'skill_gate_audit (REFUSE)';
          } else {
            stage = 'PASSED'; stage_source = 'skill_gate_audit (PRODUCE)';
          }
        }
        out.stage_counts[stage] += 1;
        out.prs.push({ repo: pr.repo, pr_number: pr.pr_number, head_sha: g.head_sha,
          run_id: g.run_id, stage, stage_source, updated_at: g.latest_at });
      }
    }
    if (!headRuns.some((hr) => hr.runs.length)) {
      // PR 无回执（仅票据/仓库行）——如实 REVIEWING with no-receipt note
      out.stage_counts.REVIEWING += 1;
      out.prs.push({ repo: pr.repo, pr_number: pr.pr_number, head_sha: pr.heads[0]?.head_sha ?? null,
        run_id: null, stage: 'REVIEWING', stage_source: 'no receipts on record', updated_at: null });
    }
  }
  // 仓库分布
  const byRepo = new Map();
  for (const r of out.prs) {
    if (!byRepo.has(r.repo)) byRepo.set(r.repo, { repo: r.repo, prs: new Set(), runs: 0, pending: 0 });
    const d = byRepo.get(r.repo);
    d.prs.add(r.pr_number); d.runs += 1;
    if (r.stage === 'ACTION_REQUIRED') d.pending += 1;
  }
  out.repository_counts = [...byRepo.values()].map((d) => ({ repo: d.repo, prs: d.prs.size, runs: d.runs, pending: d.pending }));
  // 趋势：近 14 天每日 run 数（receipts 最早出现日）
  // R4 修复（FB-05）：created_at 来自 pg 驱动时是 Date 对象——String(Date) 会得到
  // "Wed Sep 25 2026…" 这类串，slice(0,10) 永远匹配不上 ISO 日期桶 → 当日数据恒 0。
  // 统一 isoDayOf：字符串取前 10 位（ISO），Date 走 toISOString（UTC 日期桶）。
  const days = new Map();
  for (const g of byRun.values()) {
    const day = isoDayOf(g.latest_at);
    if (!day) continue;
    days.set(day, (days.get(day) || 0) + 1);
  }
  for (const t of out.trend) t.runs = days.get(t.date) || 0;
  return out;
}
