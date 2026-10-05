// console/backend/lib/multiuser/job-consumer.mjs — rc.11 PR-A：平台级自动 job consumer。
//
// 背景：webhook（/api/mu/github/webhook）入队的 event_sync job 此前无自动消费者，
// 需人工 POST /api/mu/jobs/tick 才消费——事件驱动审查存在"等人点 tick"缺口。
//
// 设计合同：
//  * 只消费【系统事件 job】（kinds=['event_sync']），执行单元复用 api.mjs 导出的
//    processClaimedEventSyncJob（与人工 tick 同一状态机，零复制）；
//  * 单赢家：每轮 tick 先 pg_try_advisory_lock('mu_job_consumer_tick')——拿不到即让位
//    （另一实例在消费，不算错误）；同一 job 不重复消费由 claimNextJob 的
//    FOR UPDATE SKIP LOCKED 保证；webhook 重复投递由 claimWebhookDelivery 去重保证；
//  * 失败不阻塞 HTTP：独立异步循环 + try/catch 全包，错误只进结构化状态；
//  * 日志与状态零敏感：不打印 payload/secret/token/代码正文/模型响应。
import crypto from 'node:crypto';

const CONSUMER_LOCK_KEY = 'mu_job_consumer_tick';
const TICK_BUDGET = 25; // 与人工 tick 同预算（api.mjs TICK_BUDGET 同值同语义）

export function consumerConfig(env = process.env) {
  const enabledRaw = String(env.MU_JOB_CONSUMER_ENABLED ?? '1').toLowerCase();
  const enabled = enabledRaw !== '0' && enabledRaw !== 'off' && enabledRaw !== 'false';
  // 显式设置尊重原值（下限 250ms——测试加速用）；未设置默认 45000ms（30–60s 生产区间）
  const raw = env.MU_JOB_CONSUMER_INTERVAL_MS;
  const intervalMs = raw === undefined ? 45000
    : Math.min(600000, Math.max(250, Number.isFinite(Number(raw)) && Number(raw) > 0 ? Number(raw) : 45000));
  return { enabled, intervalMs };
}

const state = {
  enabled: false,
  intervalMs: null,
  startedAt: null,
  lastTickAt: null,
  lastTickDurationMs: null,
  lastTickProcessed: 0,
  lastTickSkipped: false,
  lastErrorCode: null,
  consecutiveFailures: 0,
  totalProcessed: 0,
  totalTicks: 0,
  totalErrors: 0,
};

/** 只读状态摘要（供 manage_instance 面板/健康端点；不含任何 payload/敏感字段）。 */
export function consumerStatus() {
  return {
    enabled: state.enabled,
    interval_ms: state.intervalMs,
    started_at: state.startedAt,
    last_tick_at: state.lastTickAt,
    last_tick_duration_ms: state.lastTickDurationMs,
    last_tick_processed: state.lastTickProcessed,
    last_tick_skipped: state.lastTickSkipped,
    last_error_code: state.lastErrorCode,
    consecutive_failures: state.consecutiveFailures,
    totals: { ticks: state.totalTicks, processed: state.totalProcessed, errors: state.totalErrors },
  };
}

let muConsumerPool = null;
async function loadPg() {
  try { return await import('pg'); } catch { /* fallback below */ }
  try {
    const { createRequire } = await import('node:module');
    return createRequire(new URL('../../test/support/noop.js', import.meta.url))('pg');
  } catch { return null; }
}

async function getConsumerPool(env) {
  if (!muConsumerPool) {
    const pg = await loadPg();
    if (!pg) return null;
    muConsumerPool = new pg.Pool({ connectionString: env.CONSOLE_PG_DSN, max: 2 });
    muConsumerPool.on('error', () => {});
  }
  return muConsumerPool;
}

/**
 * 启动后台消费循环。getMuStore 与 processClaimedEventSyncJob 由调用方注入
 * （api.mjs 导出——同一状态机）。PG 不可用/实例未就绪时不 crash，记 last_error_code
 * 并在下一间隔重试。
 */
export function startJobConsumer({ env = process.env, getMuStore, processClaimedEventSyncJob }) {
  const cfg = consumerConfig(env);
  state.enabled = cfg.enabled;
  state.intervalMs = cfg.intervalMs;
  state.startedAt = new Date().toISOString();
  if (!cfg.enabled) return { enabled: false, stop() {} };
  if (!env.CONSOLE_PG_DSN) { state.enabled = false; return { enabled: false, stop() {} }; }

  let stopped = false;
  let running = false;
  const timer = setInterval(() => {
    if (stopped || running) return; // 上一轮未结束（长 tick）则跳过本间隔
    running = true;
    tickOnce({ env, getMuStore, processClaimedEventSyncJob })
      .catch((e) => {
        state.totalErrors += 1;
        state.consecutiveFailures += 1;
        state.lastErrorCode = 'consumer_tick_error:' + String(e?.code ?? e?.message ?? e).slice(0, 60);
      })
      .finally(() => { running = false; });
  }, cfg.intervalMs);
  timer.unref?.();

  async function tickOnce({ env: e, getMuStore: getStore, processClaimedEventSyncJob: processJob }) {
    state.totalTicks += 1;
    state.lastTickAt = new Date().toISOString();
    const t0 = Date.now();
    const pool = await getConsumerPool(e);
    if (!pool) { state.lastErrorCode = 'consumer_pg_module_unavailable'; throw new Error('pg unavailable'); }
    const client = await pool.connect();
    try {
      // 单赢家：拿不到锁=另一实例正在消费本轮，让位（skipped，非错误）
      const lock = await client.query(`SELECT pg_try_advisory_lock(hashtext('${CONSUMER_LOCK_KEY}')) AS ok`);
      if (!lock.rows[0]?.ok) {
        state.lastTickSkipped = true;
        state.lastTickProcessed = 0;
        state.lastErrorCode = null;
        state.consecutiveFailures = 0;
        return;
      }
      try {
        const store = await getStore(e);
        if (!store) { state.lastErrorCode = 'mu_store_unavailable'; throw new Error('mu store unavailable'); }
        let processed = 0;
        for (let budget = TICK_BUDGET; budget > 0; budget--) {
          if (stopped) break;
          const job = await store.claimNextJob({ kinds: ['event_sync'] });
          if (!job) break;
          const r = await processJob(store, (text, params) => pool.query(text, params), job);
          processed += 1;
          if (r?.state === 'requeued') break; // 可重试失败：留待下轮（与人工 tick 同语义）
        }
        state.lastTickSkipped = false;
        state.lastTickProcessed = processed;
        state.lastErrorCode = null;
        state.consecutiveFailures = 0;
        state.totalProcessed += processed;
      } finally {
        await client.query(`SELECT pg_advisory_unlock(hashtext('${CONSUMER_LOCK_KEY}'))`).catch(() => {});
      }
    } finally {
      client.release();
      state.lastTickDurationMs = Date.now() - t0;
    }
  }

  return {
    enabled: true,
    intervalMs: cfg.intervalMs,
    stop() { stopped = true; clearInterval(timer); state.enabled = false; },
  };
}

export const __test = { CONSUMER_LOCK_KEY, TICK_BUDGET };
