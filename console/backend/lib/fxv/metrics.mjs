// fxv/metrics.mjs — FXV 运营指标（从 fxv.attempts/audit_events 真实推导，无 mock）。
export async function fxvMetrics(dsn) {
  if (!dsn) return { source: 'BACKEND_NOT_WIRED', metrics: {} };
  const pg = await import('pg').catch(() => null);
  if (!pg) return { source: 'BACKEND_ERROR', error: 'pg module unavailable', metrics: {} };
  const c = new pg.Client({ connectionString: dsn, connectionTimeoutMillis: 3000 });
  try {
    await c.connect();
    const states = await c.query("SELECT state, count(*)::int n FROM fxv.attempts GROUP BY state");
    const totals = await c.query(`SELECT count(*)::int total,
      count(*) FILTER (WHERE state IN ('VERIFIED','DRY_RUN_COMPLETE'))::int succeeded,
      count(*) FILTER (WHERE state IN ('TEST_FAILED','ERROR_FATAL','TIMEOUT'))::int failed,
      count(*) FILTER (WHERE state = 'TIMEOUT')::int timeouts,
      count(*) FILTER (WHERE state = 'DIGEST_DRIFT')::int digest_drifts,
      count(*) FILTER (WHERE state = 'ROLLED_BACK')::int rollbacks,
      count(*) FILTER (WHERE state LIKE 'AWAITING%')::int manual_waits,
      count(*) FILTER (WHERE state_detail->>'artifact_status' = 'FAILED')::int artifact_failures FROM fxv.attempts`);
    const conflicts = await c.query("SELECT count(*)::int n FROM fxv.audit_events WHERE kind='TRANSITION' AND reason LIKE 'concurrent%'");
    const recoveries = await c.query("SELECT count(*)::int n FROM fxv.audit_events WHERE actor='recovery'");
    const archive = await c.query(`SELECT
        count(*) FILTER (WHERE kind='ARTIFACT_ARCHIVED')::int archive_success,
        count(*) FILTER (WHERE kind='ARTIFACT_COMPLETE')::int archive_complete,
        count(*) FILTER (WHERE kind='ARTIFACT_FAILED')::int archive_failure,
        count(*) FILTER (WHERE kind='ARTIFACT_FAILED' AND reason ILIKE '%MISSING%')::int missing_artifact,
        count(*) FILTER (WHERE kind='ARTIFACT_FAILED' AND (reason ILIKE '%MISMATCH%' OR reason ILIKE '%digest%'))::int digest_mismatch,
        coalesce(sum((meta->>'bytes')::bigint),0) artifact_bytes,
        coalesce(round(avg((meta->>'latency_ms')::numeric),0),0) archive_latency_avg_ms,
        coalesce(round(percentile_disc(0.95) WITHIN GROUP (ORDER BY (meta->>'latency_ms')::numeric),0),0) archive_latency_p95_ms
      FROM fxv.audit_events WHERE kind LIKE 'ARTIFACT%'`);
    const durations = await c.query(`SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (updated_at-created_at))) p50,
      percentile_disc(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (updated_at-created_at))) p95
      FROM fxv.attempts WHERE state IN ('VERIFIED','DRY_RUN_COMPLETE','TEST_FAILED','ERROR_FATAL','TIMEOUT','ROLLED_BACK')`);
    const t = totals.rows[0];
    const m = {
      attempts_total: t.total, succeeded: t.succeeded, failed: t.failed,
      success_rate: t.total ? +(t.succeeded / t.total).toFixed(3) : null,
      failure_rate: t.total ? +(t.failed / t.total).toFixed(3) : null,
      timeouts: t.timeouts, digest_drifts: t.digest_drifts, rollbacks: t.rollbacks,
      manual_waits: t.manual_waits, artifact_failures: t.artifact_failures,
      concurrent_conflicts: conflicts.rows[0].n, recoveries: recoveries.rows[0].n,
      duration_s: { p50: durations.rows[0].p50, p95: durations.rows[0].p95 },
      by_state: Object.fromEntries(states.rows.map((r) => [r.state, r.n])),
      ...archive.rows[0],
    };
    // 告警条件（明确阈值；触发时 API 返回 alerts 数组）
    const alerts = [];
    if (m.failure_rate != null && m.failure_rate > 0.3) alerts.push(`failure_rate ${(m.failure_rate*100).toFixed(0)}% > 30%`);
    if (m.artifact_failures > 0) alerts.push(`artifact_failures=${m.artifact_failures} > 0`);
    if (m.digest_drifts > 0) alerts.push(`digest_drifts=${m.digest_drifts} > 0（绑定违约需排查）`);
    if (m.timeouts > 2) alerts.push(`timeouts=${m.timeouts} > 2`);
    if (m.archive_failure > 0) alerts.push(`archive_failure=${m.archive_failure} > 0`);
    if (m.missing_artifact > 0) alerts.push(`missing_artifact=${m.missing_artifact} > 0`);
    if (m.digest_mismatch > 0) alerts.push(`digest_mismatch=${m.digest_mismatch} > 0`);
    if (Number(m.archive_latency_avg_ms) > 5000) alerts.push(`archive_latency_avg=${m.archive_latency_avg_ms}ms > 5000ms`);
    return { source: 'POSTGRESQL_LIVE', metrics: m, alerts };
  } catch (e) {
    return { source: 'BACKEND_ERROR', error: String(e?.message || e).slice(0, 200), metrics: {} };
  } finally { await c.end().catch(() => {}); }
}
