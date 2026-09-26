// fxv/api.mjs — FXV attempts 只读 API（供 Console 展示；数据源=fxv.attempts 真实状态）。
export async function fxvAttempts(dsn, { limit = 50 } = {}) {
  if (!dsn) return { source: 'BACKEND_NOT_WIRED', attempts: [] };
  const pg = await import('pg').catch(() => null);
  if (!pg) return { source: 'BACKEND_ERROR', error: 'pg module unavailable', attempts: [] };
  const client = new pg.Client({ connectionString: dsn, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    const r = await client.query(
      `SELECT attempt_id, ticket_id, repo, branch, state, updated_at, expires_at,
              state_detail->>'last_reason' AS last_reason
         FROM fxv.attempts ORDER BY updated_at DESC LIMIT $1`, [limit]);
    return { source: 'POSTGRESQL_LIVE', attempts: r.rows };
  } catch (e) {
    return { source: 'BACKEND_ERROR', error: String(e?.message || e).slice(0, 200), attempts: [] };
  } finally { await client.end().catch(() => {}); }
}
