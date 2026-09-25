// console/backend/lib/core-pilot.mjs — 核心控制面五 API 的 PG 实时状态读取
// （CANONICAL_CONSOLE_PROMOTION；迁移自 demo-platform 已验证实现）。
// 诚实语义：无 DSN → BACKEND_NOT_WIRED；连接/查询失败 → BACKEND_ERROR（带错误码，
// 永不假成功）；成功 → POSTGRESQL_LIVE。JSONB payload 抽取、顺序查询（单 client
// 不支持并发）、仅缓存 LIVE 结果（错误态每次重探）。Node pg 为可选依赖：
// 未安装时按 NOT_WIRED 处理（不伪造）。
const CACHE_TTL_MS = 5000;
let cache = { data: null, at: 0 };

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
