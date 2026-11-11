// console/backend/lib/multiuser/agents/matrix-sync-listener.mjs — PR-A：worker→console
// 结果回传推送通道（Matrix /sync 事件流长轮询），替代 collectReply 的房间消息轮询拉取。
//
// 职责：常驻 /_matrix/client/v3/sync 长轮询（timeout=30s + timeline filter 减载），
// 把 rooms.join.*.timeline.events 中的 m.room.message 事件按 (roomId, marker) 精确
// 匹配给进程内 waiter 并即时结算——回复可见延迟从 replyPollMs 轮询间隔压到事件到达。
//
// 安全契约（与 matrix-transport 四重绑定一致，防御语义不减）：
//  * waiter 绑定 = 固定 roomId + 精确 sender + origin_server_ts 时间窗 + body 含
//    submissionId marker + body 可提取平衡 JSON（与 collectReply 的 extractReplyJson
//    同源）——任一不满足一律不结算；多房间互不串扰（roomId 维度天然隔离）；
//  * 幂等：每 waiter 至多结算一次（结算即摘除）+ eventId 小型 LRU（结算/命中无 JSON
//    的事件记入——/sync 重连同批重放不再重复处理；未匹配事件不记入，留给尚未注册的
//    waiter 被后续重放送达——at-least-once 语义）；
//  * since 游标持久化：每批成功后 tmp+rename 原子写（env MU_MATRIX_SYNC_CURSOR_PATH，
//    默认 os.tmpdir()/mp-matrix-sync-cursor.json）；启动读文件，无 token 从 null 起步；
//  * 断线续传：连接错误退避重试，始终从持久化 token 续传——homeserver 会把停机窗口
//    事件补发（零丢失的机制基础）；401 清缓存重登（matrix-transport.invalidateMatrixLogin）；
//  * round 级 trace（脱敏，AT_SYNC_DIAG=1，风格对齐 AT_COLLECT_DIAG）：roomId/sender/
//    origin_server_ts/marker 命中/结算结果——绝不输出 token、URL、密码、消息正文全文
//    （marker 本身无敏感内容可输出）；
//  * 健康标志：未启动或连续失败 ≥ failThreshold = unhealthy（isMatrixSyncListenerHealthy）；
//    一切错误只记日志，绝不 crash server；orchestrator 在 unhealthy（含未启动）时立即
//    短路回落 collectReply 纯轮询（fail-closed 零回归）。
// 单实例假设：console 进程内单例（与 job-consumer 同模型）；多进程部署各自持游标文件，
// 不在本通道防护范围。
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import * as mt from './matrix-transport.mjs';

export const MSL_LIMITS = Object.freeze({
  syncLongPollMs: 30_000,   // /sync timeout 参数（长轮询服务端挂起上限）
  retryBaseMs: 2_000,       // 断线退避基数
  retryMaxMs: 30_000,       // 断线退避上限
  failThreshold: 3,         // 连续失败 ≥ 此值 → unhealthy
  sweepMs: 20_000,          // orchestrator 回落 collectReply 的有界对账窗（15-30s 带内）
  maxSeenEventIds: 512,     // eventId LRU 容量（重放防护窗口）
  waiterMaxMs: 600_000,     // waiter 超时硬顶（防御异常入参）
  reqTimeoutMs: 45_000,     // /sync 单请求超时（长轮询 30s + 网络余量；真取消防悬挂）
});

// 服务端 timeline filter：只要 m.room.message、限量——降低 homeserver 负载与批次体积
//（未随批取全的事件由下次 since 续传，无丢失）。
const MSL_SYNC_FILTER = JSON.stringify({ room: { timeline: { limit: 20, types: ['m.room.message'] } } });

/** 游标文件路径（env 仅新增读取：MU_MATRIX_SYNC_CURSOR_PATH，缺省=系统临时目录）。 */
export function resolveSyncCursorPath(env = process.env) {
  const p = String(env.MU_MATRIX_SYNC_CURSOR_PATH ?? '').trim();
  return p || path.join(os.tmpdir(), 'mp-matrix-sync-cursor.json');
}

// ── 诊断（脱敏契约）──
const diagOn = () => String(process.env.AT_SYNC_DIAG ?? '') === '1';
const trace = (obj) => { if (diagOn()) console.log(`[mt-sync] ${JSON.stringify(obj)}`); };
// 错误只留稳定类别；URL 形态串一律抹除（token 随 query 在 URL 中——同被抹除）
const sanitizeErr = (e) => String(e?.code ?? e?.message ?? e).replace(/https?:\/\/\S+/g, '[url]').slice(0, 120);

// ── 游标持久化（tmp+rename 原子写，风格对齐 rag-model-download 的断点续传）──
function readCursorSince(cursorPath) {
  try {
    const j = JSON.parse(fs.readFileSync(cursorPath, 'utf8'));
    return (typeof j?.since === 'string' && j.since) ? j.since : null;
  } catch { return null; } // 无文件/损坏 → 从 null 起步（首次初始同步建立游标）
}
function writeCursorSince(cursorPath, since) {
  const tmp = `${cursorPath}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify({ since, updatedAt: new Date().toISOString() }));
  fs.renameSync(tmp, cursorPath); // 同目录原子替换
}

// ── /sync 单请求超时（AbortController 真取消 + race 兜底；长轮询预算 > syncLongPollMs）──
const mslTimeoutError = (scope) => { const e = new Error(`MSL_${scope}_TIMEOUT`); e.code = 'MSL_REQ_TIMEOUT'; return e; };
function syncFetchWithTimeout(fetchImpl, ms) {
  return async (url) => {
    const ctrl = new AbortController();
    let timer = null;
    try {
      return await Promise.race([
        Promise.resolve(fetchImpl(url, { signal: ctrl.signal })).catch((e) => {
          if (ctrl.signal.aborted) throw mslTimeoutError('REQ'); // abort 引发的拒绝统一为超时
          throw e;
        }),
        new Promise((_, reject) => {
          timer = setTimeout(() => { ctrl.abort(); reject(mslTimeoutError('REQ')); }, ms);
        }),
      ]);
    } finally { if (timer) clearTimeout(timer); }
  };
}

// ── 进程内单例状态 ──
let state = null;

/** 健康查询：未启动 / 连续失败达阈值 → false（orchestrator 据此立即短路回落纯轮询）。 */
export function isMatrixSyncListenerHealthy() {
  if (!state || !state.started) return false;
  return state.consecutiveFailures < MSL_LIMITS.failThreshold;
}

/** 诊断快照（零敏感字段——供测试/运维观测；错误只留脱敏类别）。 */
export function matrixSyncListenerState() {
  if (!state) return { started: false, healthy: false, pendingWaiters: 0 };
  let pending = 0;
  for (const set of state.waiters.values()) pending += set.size;
  return {
    started: state.started,
    healthy: isMatrixSyncListenerHealthy(),
    consecutiveFailures: state.consecutiveFailures,
    lastError: state.lastError,
    lastOkAt: state.lastOkAt,
    sinceSet: Boolean(state.since),
    pendingWaiters: pending,
    seenEventIds: state.seen.size,
    batches: state.batches,
    eventsSeen: state.eventsSeen,
    settled: state.settled,
  };
}

/**
 * 注册回复 waiter（orchestrator 在 sendTaskDelegation 之前调用——先挂监听再派发，
 * 事件早到也能结算）。返回 { promise, cancel }；promise 恒 resolve 不 reject：
 *  * { ok:true, event:{ eventId, sender, originServerTs, body, json } } —— 结算（json
 *    与 extractReplyJson 输出同源，调用方直接进入既有 schema 校验链）；
 *  * { ok:false, reason:'MSL_TIMEOUT' | 'MSL_CANCELLED' | 'MSL_NOT_STARTED' }。
 * 未启动时返回立即失败的 waiter（调用方应先查健康标志——此为防御性短路）。
 */
export function registerReplyWaiter({ roomId, expectedSender, marker, sinceTs, timeoutMs } = {}) {
  const room = String(roomId ?? '');
  const sender = String(expectedSender ?? '');
  const mark = String(marker ?? '');
  const since = Number(sinceTs ?? 0);
  const budget = Math.min(Math.max(Number(timeoutMs) || MSL_LIMITS.waiterMaxMs, 1), MSL_LIMITS.waiterMaxMs);
  if (!state || !state.started) {
    return { promise: Promise.resolve({ ok: false, reason: 'MSL_NOT_STARTED' }), cancel: () => {} };
  }
  const st = state;
  let settled = false;
  let timer = null;
  let resolveFn = null;
  const promise = new Promise((res) => { resolveFn = res; });
  const set = st.waiters.get(room) ?? new Set();
  const w = {
    roomId: room, sender, marker: mark, sinceTs: since, promise,
    cancel: () => finish({ ok: false, reason: 'MSL_CANCELLED' }),
  };
  function finish(result) {
    if (settled) return;
    settled = true;
    if (timer) { clearTimeout(timer); timer = null; }
    set.delete(w);
    if (!set.size && st.waiters.get(room) === set) st.waiters.delete(room);
    resolveFn(result);
  }
  w._finish = finish; // 结算/停止路径经此收口（settleWaiters 与 stopMatrixSyncListener 调用）
  timer = setTimeout(() => finish({ ok: false, reason: 'MSL_TIMEOUT' }), budget);
  if (typeof timer.unref === 'function') timer.unref(); // 绝不因 waiter 阻止进程退出
  set.add(w);
  st.waiters.set(room, set);
  trace({ op: 'waiter_registered', room, marker: mark, budget_ms: budget });
  return w;
}

// 结算匹配：marker 命中（body includes）+ sender 精确 + ts 时间窗 + 平衡 JSON 全满足才结算。
// marker 命中但无 JSON → 不结算保持等待（与 collectReply「等下一条」语义一致）。
function settleWaiters(st, roomId, ev, body) {
  const set = st.waiters.get(roomId);
  let matchedMarker = null;
  let sawNoJson = false;
  if (!set || !set.size) return { status: 'no_waiter', marker: null };
  for (const w of set) {
    if (!body.includes(w.marker)) continue;
    matchedMarker = w.marker;
    if (String(ev.sender ?? '') !== w.sender) continue;
    if (!(Number(ev.origin_server_ts ?? 0) > w.sinceTs)) continue;
    const json = mt.extractReplyJson(body);
    if (!json) { sawNoJson = true; continue; }
    w._finish({
      ok: true,
      event: {
        eventId: String(ev.event_id ?? ''),
        sender: String(ev.sender ?? ''),
        originServerTs: Number(ev.origin_server_ts ?? 0),
        body,
        json,
      },
    });
    return { status: 'settled', marker: matchedMarker };
  }
  return { status: sawNoJson ? 'matched_no_json' : 'no_waiter', marker: matchedMarker };
}

function handleRoomMessage(st, roomId, ev) {
  if (ev?.type !== 'm.room.message') return;
  st.eventsSeen++;
  const eventId = String(ev?.event_id ?? '');
  // LRU 重放防护：只拦「已结算/已命中」过的 eventId——未匹配事件不记入，重连重放
  // 可送达后注册的 waiter（at-least-once）；双投递不可能双结算（结算即摘除）。
  if (eventId && st.seen.has(eventId)) {
    if (diagOn()) trace({ room: roomId, event: eventId.slice(0, 16), marker: null, marker_hit: false, outcome: 'replayed_skip' });
    return;
  }
  const body = String(ev?.content?.body ?? '');
  const res = settleWaiters(st, roomId, ev, body);
  if ((res.status === 'settled' || res.status === 'matched_no_json') && eventId) {
    st.seen.add(eventId);
    while (st.seen.size > MSL_LIMITS.maxSeenEventIds) {
      for (const k of st.seen) { st.seen.delete(k); break; } // 淘汰最旧（Set 保插入序）
    }
  }
  if (res.status === 'settled') st.settled++;
  trace({ room: roomId, sender: String(ev?.sender ?? ''), ts: Number(ev?.origin_server_ts ?? 0),
    event: eventId.slice(0, 16), marker: res.marker, marker_hit: Boolean(res.marker),
    outcome: res.status });
}

function processSyncPayload(st, j) {
  const joins = j?.rooms?.join ?? {};
  let seenMessages = 0;
  for (const roomId of Object.keys(joins)) {
    const events = joins[roomId]?.timeline?.events ?? [];
    if (!Array.isArray(events)) continue;
    for (const ev of events) {
      if (ev?.type === 'm.room.message') seenMessages++;
      handleRoomMessage(st, String(roomId), ev);
    }
  }
  return seenMessages;
}

// ── 常驻循环：长轮询 /sync → 推进游标（原子持久化）→ 事件结算；错误退避续传 ──
async function runLoop(st) {
  const doFetch = syncFetchWithTimeout(st.fetchImpl, MSL_LIMITS.reqTimeoutMs);
  let backoff = MSL_LIMITS.retryBaseMs;
  while (!st.stopped) {
    // 每轮强制让出事件循环（check 阶段，pending timers 照常触发）——防即时响应的
    // 服务端/stub 造成微任务热旋转饿死 timers（生产长轮询挂起天然有间隔，此为纯防御；
    // 成功与失败退避两条路径都经此让出）
    await new Promise((r) => setImmediate(r));
    if (st.stopped) break;
    try {
      const lg = await mt.matrixLogin(st.cfg, st.fetchImpl); // 内部自包 LOGIN 超时
      if (!lg.ok) throw new Error(`MSL_LOGIN_${String(lg.reason ?? 'FAILED')}`);
      const firstSync = !st.since;
      const url = `${st.cfg.baseUrl}/_matrix/client/v3/sync?timeout=${firstSync ? 0 : MSL_LIMITS.syncLongPollMs}`
        + (st.since ? `&since=${encodeURIComponent(st.since)}` : '')
        + `&filter=${encodeURIComponent(MSL_SYNC_FILTER)}`
        + `&access_token=${lg.token}`;
      const r = await doFetch(url);
      if (r && r.status === 401) { mt.invalidateMatrixLogin(); throw new Error('MSL_SYNC_HTTP_401'); }
      if (!r || !r.ok) throw new Error(`MSL_SYNC_HTTP_${r?.status ?? 'NONE'}`);
      const j = await r.json().catch(() => null);
      if (!j || typeof j !== 'object' || !j.next_batch) throw new Error('MSL_SYNC_BAD_PAYLOAD');
      st.since = String(j.next_batch);
      if (st.since !== st.writtenSince) { // 幂等跳过（同 token 重复批次不重写盘）
        try { writeCursorSince(st.cursorPath, st.since); st.writtenSince = st.since; }
        catch (e) { trace({ op: 'cursor_write_failed', kind: sanitizeErr(e) }); } // 写失败不致命：至多重启后重放（LRU+绑定过滤兜底）
      }
      const yielded = processSyncPayload(st, j);
      st.consecutiveFailures = 0;
      st.lastError = null;
      st.lastOkAt = Date.now();
      st.batches++;
      backoff = MSL_LIMITS.retryBaseMs;
      // 生产节奏由长轮询挂起承担；stub/空批场景 idleMs 兜底防热旋转（测试注入）
      if (!yielded && st.idleMs > 0) await st.sleepImpl(st.idleMs);
    } catch (e) {
      st.consecutiveFailures++;
      st.lastError = sanitizeErr(e);
      trace({ op: 'sync_failed', failures: st.consecutiveFailures, kind: st.lastError });
      const wait = backoff;
      backoff = Math.min(backoff * 2, MSL_LIMITS.retryMaxMs);
      await st.sleepImpl(wait); // 退避重试——始终从持久化 since 续传（停机窗口由 homeserver 补发）
    }
  }
}

/**
 * 启动监听（进程内单例，重复启动幂等返回 MSL_ALREADY_STARTED）。参数全可注入：
 * cfg（mt.resolveMatrixConfig 输出形状）/ fetchImpl / cursorPath / sleepImpl / idleMs。
 * 循环意外退出只置 unhealthy 并记日志——绝不 crash server。
 */
export function startMatrixSyncListener({ cfg, fetchImpl = fetch, cursorPath, sleepImpl, idleMs = 0, env = process.env } = {}) {
  if (state && state.started) return { ok: false, reason: 'MSL_ALREADY_STARTED' };
  if (!cfg || cfg.kind !== 'matrix' || !cfg.baseUrl) return { ok: false, reason: 'MSL_NOT_CONFIGURED' };
  const cp = cursorPath ?? resolveSyncCursorPath(env);
  const since = readCursorSince(cp); // 无 token → null 起步（首次初始同步）
  const st = {
    started: true, stopped: false, cfg, fetchImpl,
    sleepImpl: sleepImpl ?? ((ms) => new Promise((r2) => setTimeout(r2, ms))),
    idleMs: Math.max(0, Number(idleMs) || 0),
    cursorPath: cp, since, writtenSince: since,
    consecutiveFailures: 0, lastError: null, lastOkAt: null,
    waiters: new Map(), seen: new Set(),
    batches: 0, eventsSeen: 0, settled: 0,
    startedAt: Date.now(),
    loopPromise: null,
  };
  state = st;
  st.loopPromise = runLoop(st).catch((e) => {
    // runLoop 逐错误内捕——此处仅兜循环体外的意外：置 unhealthy（orchestrator 回落纯轮询）
    console.error('[mt-sync] loop exited unexpectedly:', sanitizeErr(e));
    st.started = false;
  });
  trace({ op: 'started', cursor_restored: Boolean(since) });
  return { ok: true, cursorRestored: Boolean(since) };
}

/** 部署级入口：Matrix env 配置齐才启动（server.mjs 接线用；缺配置如实返回 reason）。 */
export function startMatrixSyncListenerFromEnv(env = process.env) {
  const c = mt.resolveMatrixConfig(env);
  if (c.kind !== 'matrix') return { ok: false, reason: c.reason };
  return startMatrixSyncListener({ cfg: c, env });
}

/** 停止（测试/运维）：摘除全部 waiter（MSL_CANCELLED）、置停标志；等待循环退出（有界）。 */
export async function stopMatrixSyncListener() {
  const st = state;
  if (!st) return;
  st.stopped = true;
  for (const set of st.waiters.values()) {
    for (const w of [...set]) { try { w._finish({ ok: false, reason: 'MSL_CANCELLED' }); } catch { /* 已结算 */ } }
  }
  st.waiters.clear();
  state = null;
  trace({ op: 'stopped' });
  // 有界等待循环退出（生产中长轮询挂起最长 reqTimeoutMs；测试注入 stub 即刻收敛）
  await Promise.race([
    st.loopPromise.catch(() => {}),
    new Promise((r) => { const t = setTimeout(r, 3_000); if (typeof t.unref === 'function') t.unref(); }),
  ]);
}

/** 测试复位（不等待循环——测试须先 stopMatrixSyncListener）。 */
export function __resetMatrixSyncListenerForTests() { state = null; }
