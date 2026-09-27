// ragtrial/queue.mjs — RAG 持久任务队列（PRODUCTION_RAG_READINESS）。
//
// PG 表 ragtrial.jobs 为唯一事实源：enqueue/claim/heartbeat/complete/fail/
// dead-letter/requeue 全部落库，worker 崩溃或 PG 重启后任务不丢。
//
// 语义合同：
//  * 幂等：dedupe_key = sha256(kind|repo|branch|doc_path|content_sha256|model_id)
//    UNIQUE——同任务重复入队返回既有 job（created=false）；
//  * 重试：fail() 按 attempts 指数退避写回 queued；attempts ≥ max → dead；
//  * 超时/崩溃恢复：claim() 可重占 heartbeat_at 过期的 running 行（stale reclaim）；
//    执行器幂等（ingest 同内容 sha → unchanged），重占后重跑不产生重复索引；
//  * 死信：state='dead' + last_error；requeueDead() 人工复活（attempts 清零）；
//  * 全生命周期审计：JOB_ENQUEUE/START/RETRY/DEAD/DONE/REQUEUE/RECLAIM 写
//    ragtrial.audit_events（actor NOT NULL）。
//
// 陈旧阈值：STALE_AFTER_MS（默认 90s）——running 行心跳超过该时长即可被重占。

import crypto from 'node:crypto';

export const JOB_KINDS = ['ingest_doc', 'delete_doc'];
export const JOB_STATES = ['queued', 'running', 'done', 'dead'];

export function createJobQueue({ pool, staleAfterMs = 90_000 } = {}) {
  if (!pool || typeof pool.query !== 'function') {
    throw new Error('createJobQueue: pool with .query() required');
  }
  const q = (text, params) => pool.query(text, params);

  async function audit(kind, actor, detail) {
    if (!actor) throw new Error('queue audit: actor required');
    await q(
      `INSERT INTO ragtrial.audit_events (kind, actor, repo, branch, detail)
       VALUES ($1,$2,$3,$4,$5::jsonb)`,
      [kind, actor, detail.repo ?? null, detail.branch ?? null, JSON.stringify(detail)]);
  }

  function dedupeKey({ kind, repo, branch, doc_path, content_sha256, model_id }) {
    return crypto.createHash('sha256')
      .update([kind, repo, branch, doc_path ?? '', content_sha256 ?? '', model_id].join('|'))
      .digest('hex');
  }

  // 幂等入队：dedupe_key 冲突返回既有任务（不新建、不重置状态）
  async function enqueue(job, { actor = 'rag-trial-operator' } = {}) {
    if (!JOB_KINDS.includes(job.kind)) throw new Error(`unknown job kind: ${job.kind}`);
    for (const f of ['kind', 'repo', 'branch', 'model_id']) {
      if (!job[f]) throw new Error(`enqueue: missing ${f}`);
    }
    const key = dedupeKey(job);
    const r = await q(
      `INSERT INTO ragtrial.jobs (kind, repo, branch, doc_path, content_sha256, model_id, dedupe_key, payload, timeout_ms, max_attempts)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10)
       ON CONFLICT (dedupe_key) DO NOTHING
       RETURNING job_id`,
      [job.kind, job.repo, job.branch, job.doc_path ?? null, job.content_sha256 ?? null,
       job.model_id, key, JSON.stringify(job.payload ?? {}),
       Number(job.timeout_ms ?? 120_000), Number(job.max_attempts ?? 5)]);
    if (r.rows.length) {
      await audit('JOB_ENQUEUE', actor, {
        repo: job.repo, branch: job.branch,
        detail: { job_id: r.rows[0].job_id, kind: job.kind, doc_path: job.doc_path ?? null, model_id: job.model_id },
      });
      return { job_id: r.rows[0].job_id, created: true };
    }
    const cur = await q(`SELECT job_id, state, attempts FROM ragtrial.jobs WHERE dedupe_key=$1`, [key]);
    return { job_id: cur.rows[0].job_id, created: false, state: cur.rows[0].state, idempotent: true };
  }

  // 认领：queued 且到期，或 running 心跳过期的（崩溃回收）。FOR UPDATE SKIP LOCKED。
  // 注意：was_stale 必须在 CTE 里基于【更新前】行计算——RETURNING 看到的是新行。
  async function claim({ worker, limit = 1 } = {}) {
    if (!worker) throw new Error('claim: worker name required');
    const r = await q(
      `WITH candidates AS (
         SELECT job_id, (heartbeat_at < now() - ($2 || ' milliseconds')::interval) AS was_stale
           FROM ragtrial.jobs
          WHERE (state='queued' AND next_run_at <= now())
             OR (state='running' AND heartbeat_at < now() - ($2 || ' milliseconds')::interval)
          ORDER BY next_run_at
          FOR UPDATE SKIP LOCKED
          LIMIT $3)
       UPDATE ragtrial.jobs j
          SET state='running', locked_by=$1, locked_at=now(),
              heartbeat_at=now(), attempts=attempts+1, updated_at=now()
         FROM candidates c
        WHERE j.job_id = c.job_id
       RETURNING j.*, c.was_stale`,
      [worker, String(staleAfterMs), Math.min(limit, 10)]);
    for (const row of r.rows) {
      await audit(row.was_stale ? 'JOB_RECLAIM' : 'JOB_START', worker, {
        repo: row.repo, branch: row.branch,
        detail: { job_id: row.job_id, kind: row.kind, attempts: row.attempts, ...(row.was_stale ? { reclaimed_from: row.locked_by } : {}) },
      });
    }
    return r.rows;
  }

  async function heartbeat(jobId, worker) {
    const r = await q(
      `UPDATE ragtrial.jobs SET heartbeat_at=now(), updated_at=now()
        WHERE job_id=$1 AND state='running' AND locked_by=$2
        RETURNING job_id`, [jobId, worker]);
    return r.rowCount === 1;
  }

  async function complete(jobId, { worker, result = null } = {}) {
    const r = await q(
      `UPDATE ragtrial.jobs SET state='done', done_at=now(), updated_at=now(),
              last_error=NULL, result=$2::jsonb
        WHERE job_id=$1 AND state='running'
        RETURNING repo, branch, kind, attempts`,
      [jobId, JSON.stringify(result ?? {})]);
    if (!r.rows.length) return { ok: false, reason: 'not_running' };
    await audit('JOB_DONE', worker ?? 'rag-worker', {
      repo: r.rows[0].repo, branch: r.rows[0].branch,
      detail: { job_id: jobId, kind: r.rows[0].kind, attempts: r.rows[0].attempts },
    });
    return { ok: true };
  }

  // 失败：未达 max → 退避重排（指数 2^attempts·5s，上限 5min）；达 max → 死信
  async function fail(jobId, error, { worker } = {}) {
    const msg = String(error?.message ?? error).slice(0, 300);
    const r = await q(
      `UPDATE ragtrial.jobs
         SET state = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
             next_run_at = now() + (LEAST(300, 5 * power(2, attempts)) || ' seconds')::interval,
             last_error = $2, updated_at = now(), locked_by=NULL, locked_at=NULL
       WHERE job_id=$1 AND state='running'
       RETURNING repo, branch, kind, attempts, max_attempts, state`,
      [jobId, msg]);
    if (!r.rows.length) return { ok: false, reason: 'not_running' };
    const row = r.rows[0];
    await audit(row.state === 'dead' ? 'JOB_DEAD' : 'JOB_RETRY', worker ?? 'rag-worker', {
      repo: row.repo, branch: row.branch,
      detail: { job_id: jobId, kind: row.kind, attempts: row.attempts, max: row.max_attempts, error: msg },
    });
    return { ok: true, state: row.state, attempts: row.attempts };
  }

  // 死信复活（人工/operator）：attempts 清零重新排队
  async function requeueDead(jobId, { actor = 'rag-trial-operator' } = {}) {
    const r = await q(
      `UPDATE ragtrial.jobs SET state='queued', attempts=0, next_run_at=now(),
              locked_by=NULL, locked_at=NULL, updated_at=now()
        WHERE job_id=$1 AND state='dead'
        RETURNING repo, branch, kind`,
      [jobId]);
    if (!r.rows.length) return { ok: false, reason: 'not_dead' };
    await audit('JOB_REQUEUE', actor, {
      repo: r.rows[0].repo, branch: r.rows[0].branch,
      detail: { job_id: jobId, kind: r.rows[0].kind },
    });
    return { ok: true };
  }

  async function list({ state = null, limit = 50 } = {}) {
    const r = state
      ? await q(`SELECT * FROM ragtrial.jobs WHERE state=$1 ORDER BY updated_at DESC LIMIT $2`, [state, Math.min(limit, 200)])
      : await q(`SELECT * FROM ragtrial.jobs ORDER BY updated_at DESC LIMIT $1`, [Math.min(limit, 200)]);
    return r.rows;
  }

  async function stats() {
    const byState = await q(
      `SELECT state, count(*)::int n,
              coalesce(sum(attempts))::int total_attempts,
              coalesce(max(attempts))::int max_attempts_seen
         FROM ragtrial.jobs GROUP BY state`);
    const dead = await q(
      `SELECT job_id, kind, repo, branch, doc_path, attempts, last_error, updated_at
         FROM ragtrial.jobs WHERE state='dead' ORDER BY updated_at DESC LIMIT 20`);
    const ages = await q(
      `SELECT coalesce(round(EXTRACT(EPOCH FROM (now() - MIN(next_run_at))))::int, 0) oldest_queued_s
         FROM ragtrial.jobs WHERE state='queued'`);
    return {
      by_state: Object.fromEntries(byState.rows.map((r) => [r.state, r.n])),
      total_attempts: byState.rows.reduce((s, r) => s + Number(r.total_attempts ?? 0), 0),
      max_attempts_seen: byState.rows.reduce((s, r) => Math.max(s, Number(r.max_attempts_seen ?? 0)), 0),
      oldest_queued_s: ages.rows[0].oldest_queued_s ?? 0,
      dead_letters: dead.rows,
    };
  }

  return { enqueue, claim, heartbeat, complete, fail, requeueDead, list, stats, audit };
}
