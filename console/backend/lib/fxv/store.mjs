// fxv/store.mjs — PG 持久化：attempts（绑定+状态）、audit_events（全转迁移审计）、
// grants（GitHub 写入显式人工授权）。幂等创建、乐观并发（expected-state 条件更新）。
// schema 由本模块幂等初始化（fxv 命名空间——console 首个自持 schema）。

export const FXV_SCHEMA_SQL = [
  `CREATE SCHEMA IF NOT EXISTS fxv`,
  `CREATE TABLE IF NOT EXISTS fxv.attempts (
     attempt_id   TEXT PRIMARY KEY,
     ticket_id    TEXT NOT NULL UNIQUE,
     finding_id   TEXT NOT NULL,
     repo         TEXT NOT NULL,
     branch       TEXT NOT NULL,
     base_head_sha TEXT NOT NULL,
     patch_digest TEXT NOT NULL,
     receipt_id   TEXT,
     state        TEXT NOT NULL,
     state_detail JSONB NOT NULL DEFAULT '{}'::jsonb,
     attempts_count INT NOT NULL DEFAULT 0,
     created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
     updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
     expires_at   TIMESTAMPTZ
   )`,
  `CREATE TABLE IF NOT EXISTS fxv.audit_events (
     seq        BIGSERIAL PRIMARY KEY,
     attempt_id TEXT NOT NULL,
     kind       TEXT NOT NULL,
     from_state TEXT,
     to_state   TEXT,
     actor      TEXT NOT NULL,
     reason     TEXT,
     meta       JSONB NOT NULL DEFAULT '{}'::jsonb,
     created_at TIMESTAMPTZ NOT NULL DEFAULT now()
   )`,
  `CREATE TABLE IF NOT EXISTS fxv.grants (
     grant_id   TEXT PRIMARY KEY,
     operator   TEXT NOT NULL,
     repo       TEXT NOT NULL,
     branch     TEXT,
     created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
     expires_at TIMESTAMPTZ NOT NULL,
     used_at    TIMESTAMPTZ
   )`,
  `CREATE INDEX IF NOT EXISTS fxv_audit_attempt_idx ON fxv.audit_events (attempt_id, seq)`,
  `CREATE INDEX IF NOT EXISTS fxv_attempts_state_idx ON fxv.attempts (state)`,
];

export async function createFxvStore({ pool }) {
  if (!pool || typeof pool.query !== 'function') {
    throw new Error('createFxvStore: pool with .query() required (pg Pool)');
  }
  const q = (text, params) => pool.query(text, params);

  async function initSchema() {
    for (const sql of FXV_SCHEMA_SQL) await q(sql);
    return true;
  }

  // 幂等建案：同 ticket_id 只建一次；返回现存或新建的 attempt
  async function fileAttempt(a) {
    for (const k of ['attempt_id', 'ticket_id', 'finding_id', 'repo', 'branch', 'base_head_sha', 'patch_digest']) {
      if (!a[k]) throw new Error(`fileAttempt: missing ${k}`);
    }
    const res = await q(
      `INSERT INTO fxv.attempts (attempt_id, ticket_id, finding_id, repo, branch, base_head_sha, patch_digest, receipt_id, state, state_detail, expires_at)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'FILED','{}'::jsonb, now() + ($9 || ' milliseconds')::interval)
       ON CONFLICT (ticket_id) DO NOTHING
       RETURNING *`, [a.attempt_id, a.ticket_id, a.finding_id, a.repo, a.branch, a.base_head_sha, a.patch_digest, a.receipt_id ?? null, String(a.approval_ttl_ms ?? 86400_000)]);
    if (res.rows.length > 0) {
      await recordEvent({ attempt_id: a.attempt_id, kind: 'FILED', to_state: 'FILED', actor: a.actor || 'control-plane', reason: a.reason || 'ticket filed' });
      return { created: true, attempt: res.rows[0] };
    }
    const cur = await getAttemptByTicket(a.ticket_id);
    return { created: false, attempt: cur };
  }

  async function getAttempt(attemptId) {
    const r = await q('SELECT * FROM fxv.attempts WHERE attempt_id = $1', [attemptId]);
    return r.rows[0] ?? null;
  }
  async function getAttemptByTicket(ticketId) {
    const r = await q('SELECT * FROM fxv.attempts WHERE ticket_id = $1', [ticketId]);
    return r.rows[0] ?? null;
  }
  async function listNonTerminal(terminals) {
    const r = await q('SELECT * FROM fxv.attempts WHERE state <> ALL($1) ORDER BY created_at', [terminals]);
    return r.rows;
  }

  // 乐观并发转迁：仅当当前 state=expected 才写入；0 行=冲突/已转
  async function compareAndSetState(attemptId, expected, next, detail) {
    const r = await q(
      `UPDATE fxv.attempts
         SET state = $3, state_detail = state_detail || $4::jsonb,
             attempts_count = attempts_count + 1, updated_at = now()
       WHERE attempt_id = $1 AND state = $2
       RETURNING *`, [attemptId, expected, next, JSON.stringify(detail ?? {})]);
    return r.rows[0] ?? null;
  }

  async function recordEvent(e) {
    await q(
      `INSERT INTO fxv.audit_events (attempt_id, kind, from_state, to_state, actor, reason, meta)
       VALUES ($1,$2,$3,$4,$5,$6,$7)`,
      [e.attempt_id, e.kind, e.from_state ?? null, e.to_state ?? null, e.actor, e.reason ?? null, JSON.stringify(e.meta ?? {})]);
  }
  async function listEvents(attemptId) {
    const r = await q('SELECT * FROM fxv.audit_events WHERE attempt_id = $1 ORDER BY seq', [attemptId]);
    return r.rows;
  }

  // ── GitHub 写入授权（具名操作员 + repo 范围 + TTL + 一次性消费）──
  async function issueGrant({ grant_id, operator, repo, branch, ttl_ms }) {
    if (!grant_id || !operator || !repo) throw new Error('issueGrant: grant_id/operator/repo required');
    const r = await q(
      `INSERT INTO fxv.grants (grant_id, operator, repo, branch, expires_at)
       VALUES ($1,$2,$3,$4, now() + ($5 || ' milliseconds')::interval)
       ON CONFLICT (grant_id) DO NOTHING
       RETURNING *`, [grant_id, operator, repo, branch ?? null, String(ttl_ms)]);
    return r.rows[0] ?? (await q('SELECT * FROM fxv.grants WHERE grant_id=$1', [grant_id])).rows[0];
  }
  // 原子消费一张有效授权（未过期+未使用；SKIP LOCKED 防并发双消费）；无则 null
  async function consumeGrant(repo) {
    const r = await q(
      `UPDATE fxv.grants SET used_at = now()
        WHERE grant_id = (SELECT grant_id FROM fxv.grants
                           WHERE repo = $1 AND used_at IS NULL AND expires_at > now()
                           ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
       RETURNING *`, [repo]);
    return r.rows[0] ?? null;
  }
  async function activeGrantCount(repo) {
    const r = await q('SELECT count(*)::int AS n FROM fxv.grants WHERE repo=$1 AND used_at IS NULL AND expires_at > now()', [repo]);
    return r.rows[0].n;
  }

  async function close() { /* pool 生命周期由调用方管理 */ }

  return { initSchema, fileAttempt, getAttempt, getAttemptByTicket, listNonTerminal,
           compareAndSetState, recordEvent, listEvents,
           issueGrant, consumeGrant, activeGrantCount, close };
}
