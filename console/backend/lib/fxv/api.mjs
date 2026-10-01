// fxv/api.mjs — FXV attempts 只读 API（供 Console 展示；数据源=fxv.attempts 真实状态）。
export async function fxvAttempts(dsn, { limit = 50, pgImpl = null } = {}) {
  if (!dsn) return { source: 'BACKEND_NOT_WIRED', data_source: 'BACKEND_NOT_WIRED', attempts: [] };
  const pg = pgImpl ?? await import('pg').then((m) => m.default ?? m).catch(() => null);
  if (!pg) return { source: 'BACKEND_ERROR', data_source: 'BACKEND_ERROR', error: 'pg module unavailable', attempts: [] };
  const client = new pg.Client({ connectionString: dsn, connectionTimeoutMillis: 3000 });
  try {
    await client.connect();
    const r = await client.query(
      `SELECT attempt_id, ticket_id, repo, branch, state, updated_at, created_at, expires_at,
              attempts_count, state_detail->>'last_reason' AS last_reason,
              state_detail->>'artifact_status' AS artifact_status,
              state_detail->>'artifact_problem' AS artifact_problem,
              state_detail->'artifacts' AS artifacts,
              (SELECT count(*)::int FROM fxv.audit_events e WHERE e.attempt_id = fxv.attempts.attempt_id) AS audit_events
         FROM fxv.attempts ORDER BY updated_at DESC LIMIT $1`, [limit]);
    return { source: 'POSTGRESQL_LIVE', data_source: 'FXV_LEGACY_PERSISTENCE',
      as_of: new Date().toISOString(), attempts: r.rows };
  } catch (e) {
    if (e?.code === '42P01') {
      return { source: 'FXV_PERSISTENCE_ABSENT', data_source: 'FXV_PERSISTENCE_ABSENT',
        capability: 'fxv_persistence_not_initialized', attempts: [] };
    }
    return { source: 'BACKEND_ERROR', data_source: 'BACKEND_ERROR',
      error: String(e?.message || e).slice(0, 200), attempts: [] };
  } finally { await client.end().catch(() => {}); }
}
