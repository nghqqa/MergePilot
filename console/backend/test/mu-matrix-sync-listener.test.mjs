// console/backend/test/mu-matrix-sync-listener.test.mjs — PR-A：Matrix /sync 事件流
// 推送监听单测（CI 门内，console/backend/test/*.test.mjs glob 自动纳管；零外部依赖，
// fetch/时钟/游标路径全注入）。覆盖：
//  a) /sync 事件送达即结算（不等轮询间隔）；
//  b) 同 marker 重复事件只结算一次；
//  c) eventId 重放（LRU）双投递只结算一次 + 容量封顶；
//  d) B 房间事件永不结算 A 房间 waiter（多房间隔离）；
//  e) 游标持久化 + 断线续传零丢失（重启从持久化 token 续传、停机窗口事件补发即达、
//     失败批次不推进游标、退避重试同 token、无 .tmp 残留）；
//  g) trace 脱敏（行内零 token/URL/密码/正文全文，白名单字段正则断言）；
//  h) 健康标志语义（未启动/连续失败阈值/恢复/停止）；
//  i) waiter cancel 与未启动防御短路；
//  j) 部署级入口环境门（缺配置拒绝/重复启动幂等）；
//  m) 绑定防御：过期 ts / 错误 sender 的事件不结算（推送路径等价 collectReply M5b/M5c）；
//  l) 非法 payload 计失败退避不 crash；401 清缓存重登。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const msl = await import('../lib/multiuser/agents/matrix-sync-listener.mjs');
const mt = await import('../lib/multiuser/agents/matrix-transport.mjs');
const {
  startMatrixSyncListener, startMatrixSyncListenerFromEnv, stopMatrixSyncListener,
  registerReplyWaiter, isMatrixSyncListenerHealthy, matrixSyncListenerState,
  resolveSyncCursorPath, __resetMatrixSyncListenerForTests, MSL_LIMITS,
} = msl;

const CFG = { kind: 'matrix', baseUrl: 'https://mxsync.test', user: 'mp-admin', password: 'sync-pw' };
const ROOM_A = '!room-a:hs';
const ROOM_B = '!room-b:hs';
const SENDER = '@mergepilot-verifier:hs';
const MARKER = '[mp:run-u1:t-verify:1]';
const REPLY_BODY = `${MARKER} {"verdict":"PASS","note":"ok"}`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(cond, { timeoutMs = 5_000, stepMs = 10 } = {}) {
  const t0 = Date.now();
  for (;;) {
    if (cond()) return;
    if (Date.now() - t0 > timeoutMs) throw new Error('waitFor timeout');
    await sleep(stepMs);
  }
}

let evSeq = 0;
const msgEv = (body, { sender = SENDER, eventId = null, ts = Date.now() } = {}) => ({
  type: 'm.room.message', event_id: eventId ?? `$ev-${++evSeq}`, sender, origin_server_ts: ts,
  content: { body },
});

// /sync + /login stub：script(since, st) → { batch } 推进批次 | { status } 错误码 |
// { throw: true } 断线 | null 原位驻留（长轮询空批，同 token 重轮）。
function mkSyncFetch({ script, loginOk = true } = {}) {
  const st = { logins: 0, syncCalls: 0, last401: false };
  const fn = async (url) => {
    const u = new URL(url);
    if (u.pathname.endsWith('/login')) {
      st.logins++;
      if (!loginOk) return { status: 401, ok: false, json: async () => ({}) };
      return { status: 200, ok: true, json: async () => ({ access_token: 'tok-u' }) };
    }
    if (u.pathname.endsWith('/sync')) {
      st.syncCalls++;
      const since = u.searchParams.get('since');
      const out = script ? script(since, st) : null;
      if (out?.throw) throw new Error('sync stub failure (scripted)');
      if (out?.status != null) {
        st.last401 = out.status === 401;
        return { status: out.status, ok: out.ok ?? out.status >= 400, json: async () => ({}) };
      }
      st.last401 = false;
      const batch = out?.batch ?? { next_batch: since ?? 'S0', rooms: { join: {} } };
      return { status: 200, ok: true, json: async () => batch };
    }
    return { status: 404, ok: false, json: async () => ({}) };
  };
  fn.state = st;
  return fn;
}

function tmpDir() { return fs.mkdtempSync(path.join(os.tmpdir(), 'msl-unit-')); }
async function cleanup(dir) {
  await stopMatrixSyncListener();
  __resetMatrixSyncListenerForTests();
  mt.__resetMatrixLoginForTests();
  if (dir) fs.rmSync(dir, { recursive: true, force: true });
}

test('a) /sync 事件送达即结算 waiter（不等轮询间隔）', async () => {
  const dir = tmpDir();
  let released = false;
  const stub = mkSyncFetch({ script: (since) => {
    if (!since) return { batch: { next_batch: 'S1', rooms: { join: {} } } };
    if (since === 'S1' && released) {
      return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY)] } } } } } };
    }
    return null;
  } });
  try {
    const started = startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    assert.equal(started.ok, true);
    await waitFor(() => stub.state.syncCalls >= 1); // 初始同步已建立游标
    const w = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 5_000 });
    const t0 = Date.now();
    released = true;
    const res = await w.promise;
    assert.equal(res.ok, true, '事件送达必须即时结算 waiter');
    assert.equal(res.event.json.verdict, 'PASS');
    assert.equal(res.event.sender, SENDER);
    assert.ok(String(res.event.body).includes(MARKER));
    assert.ok(Number(res.event.originServerTs) > 0);
    assert.ok(Date.now() - t0 < 3_000, '即时结算（不等 5s 轮询间隔）');
    assert.equal(matrixSyncListenerState().settled, 1);
    assert.equal(matrixSyncListenerState().pendingWaiters, 0, '结算后 waiter 摘除');
  } finally { await cleanup(dir); }
});

test('b) 同 marker 重复事件（不同 eventId）只结算一次', async () => {
  const dir = tmpDir();
  let phase = 0;
  const stub = mkSyncFetch({ script: (since) => {
    if (!since) return { batch: { next_batch: 'S1', rooms: { join: {} } } };
    if (since === 'S1' && phase >= 1) return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$dup-1' })] } } } } } };
    if (since === 'S2' && phase >= 2) return { batch: { next_batch: 'S3', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$dup-2' })] } } } } } };
    return null;
  } });
  try {
    startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    await waitFor(() => stub.state.syncCalls >= 1);
    const w = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 5_000 });
    phase = 1;
    const res = await w.promise;
    assert.equal(res.ok, true);
    assert.equal(res.event.eventId, '$dup-1');
    phase = 2;
    await waitFor(() => matrixSyncListenerState().eventsSeen >= 2); // 第二条同 marker 事件已被处理
    assert.equal(matrixSyncListenerState().settled, 1, '重复 marker 事件不得二次结算（结算即摘除）');
  } finally { await cleanup(dir); }
});

test('c) eventId 重放（LRU）双投递只结算一次 + 容量封顶', async () => {
  const dir = tmpDir();
  let phase = 0;
  const FILL = MSL_LIMITS.maxSeenEventIds + 8;
  // 填充事件=marker 命中但无 JSON → matched_no_json（会记入 LRU）——真正触发容量淘汰
  const fillers = Array.from({ length: FILL }, (_, i) => msgEv(`${MARKER} filler ${i} without braces`, { eventId: `$fill-${i}`, ts: Date.now() + i }));
  const stub = mkSyncFetch({ script: (since) => {
    if (!since) return { batch: { next_batch: 'S1', rooms: { join: {} } } };
    if (since === 'S1' && phase >= 1) return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$rep' })] } } } } } };
    if (since === 'S2' && phase >= 2) return { batch: { next_batch: 'S3', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$rep' })] } } } } } }; // 同 id 重放
    if (since === 'S3' && phase >= 3) return { batch: { next_batch: 'S4', rooms: { join: { [ROOM_A]: { timeline: { events: fillers } } } } } }; // 挤出 LRU
    if (since === 'S4' && phase >= 4) return { batch: { next_batch: 'S5', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$rep' })] } } } } } }; // 出窗后再重放
    return null;
  } });
  try {
    startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    await waitFor(() => stub.state.syncCalls >= 1);
    const w = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 5_000 });
    phase = 1;
    const res = await w.promise;
    assert.equal(res.ok, true);
    phase = 2;
    await waitFor(() => matrixSyncListenerState().eventsSeen >= 2); // 重放被 LRU 识别并跳过
    assert.equal(matrixSyncListenerState().settled, 1, '同 eventId 重放不得双结算');
    phase = 3;
    await waitFor(() => matrixSyncListenerState().eventsSeen >= 2 + FILL);
    assert.ok(matrixSyncListenerState().seenEventIds <= MSL_LIMITS.maxSeenEventIds, 'LRU 容量封顶');
    phase = 4;
    await waitFor(() => matrixSyncListenerState().eventsSeen >= 2 + FILL + 1); // 出窗重放按未匹配处理
    assert.equal(matrixSyncListenerState().settled, 1, '出窗重放也不得双结算（waiter 早已摘除）');
  } finally { await cleanup(dir); }
});

test('d) B 房间事件永不结算 A 房间 waiter（多房间隔离）', async () => {
  const dir = tmpDir();
  let stage = 0;
  const stub = mkSyncFetch({ script: (since) => {
    if (!since) return { batch: { next_batch: 'S1', rooms: { join: {} } } };
    if (since === 'S1' && stage >= 1) return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_B]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$roomB' })] } } } } } };
    if (since === 'S2' && stage >= 2) return { batch: { next_batch: 'S3', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$roomA' })] } } } } } };
    return null;
  } });
  try {
    startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    await waitFor(() => stub.state.syncCalls >= 1);
    const w = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 5_000 });
    let settledFlag = false;
    w.promise.then((r) => { if (r?.ok) settledFlag = true; });
    stage = 1;
    await waitFor(() => matrixSyncListenerState().eventsSeen >= 1); // B 房事件已处理
    await sleep(80);
    assert.equal(settledFlag, false, 'B 房间事件不得结算 A 房间 waiter');
    assert.equal(matrixSyncListenerState().settled, 0);
    stage = 2;
    const res = await w.promise;
    assert.equal(res.ok, true);
    assert.equal(res.event.eventId, '$roomA');
    assert.equal(matrixSyncListenerState().settled, 1, '只有 A 房事件结算（恰一次）');
  } finally { await cleanup(dir); }
});

test('e) 游标持久化+断线续传零丢失：重启从持久化 token 续传、停机窗口补发即达、失败同 token 重试', async () => {
  const dir = tmpDir();
  const cursorPath = path.join(dir, 'cursor.json');
  // 阶段1：初始同步建立游标后停机（模拟服务重启）
  const stub1 = mkSyncFetch({ script: (since) => (!since ? { batch: { next_batch: 'S1', rooms: { join: {} } } } : null) });
  startMatrixSyncListener({ cfg: CFG, fetchImpl: stub1, cursorPath, idleMs: 5 });
  await waitFor(() => { try { return JSON.parse(fs.readFileSync(cursorPath, 'utf8')).since === 'S1'; } catch { return false; } });
  await cleanup(null);
  // 阶段2：以持久化游标重启——停机窗口内事件由 homeserver 补发，必须送达（零丢失）
  let consumed = false;
  let failOnce = false;
  let failAt = -1;
  const failLog = [];
  const sincies = [];
  const downBody = `${MARKER} {"verdict":"PASS","note":"downtime-window"}`;
  const downEv = () => msgEv(downBody, { eventId: '$down-1' });
  const stub2 = mkSyncFetch({ script: (since) => {
    if (!since) return { batch: { next_batch: 'S1', rooms: { join: {} } } }; // 不应走到（游标已持久化）
    sincies.push(since);
    if (failOnce) { failOnce = false; failAt = sincies.length; failLog.push(since); return { throw: true }; }
    if (since === 'S1') return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_A]: { timeline: { events: [downEv()] } } } } } };
    if (since === 'S2' && !consumed) return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_A]: { timeline: { events: [downEv()] } } } } } }; // 补发窗（at-least-once）
    return null;
  } });
  try {
    const started = startMatrixSyncListener({ cfg: CFG, fetchImpl: stub2, cursorPath, idleMs: 5, sleepImpl: async () => {} });
    assert.equal(started.ok, true);
    assert.equal(started.cursorRestored, true, '启动时应报告游标已恢复');
    await waitFor(() => sincies.length >= 1);
    assert.equal(sincies[0], 'S1', '重启后首个 /sync 必须携带持久化游标（非 null 起步）');
    const w2 = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 5_000 });
    const res2 = await w2.promise;
    assert.equal(res2.ok, true, '停机窗口事件补发必须送达 waiter（零丢失）');
    assert.equal(res2.event.json.note, 'downtime-window');
    consumed = true;
    assert.equal(JSON.parse(fs.readFileSync(cursorPath, 'utf8')).since, 'S2', '批成功后游标原子推进');
    assert.ok(fs.readdirSync(dir).every((f) => !f.includes('.tmp')), '原子写不得残留 .tmp');
    // 断线一次：退避重试必须从同一持久化 token（S2）续传——失败批次不推进游标
    failOnce = true;
    await waitFor(() => failAt >= 0 && sincies.length >= failAt + 2, { timeoutMs: 5_000 });
    assert.equal(failLog.length, 1, '恰一次脚本化断线');
    assert.equal(failLog[0], 'S2', '断线发生在持久化 token S2 上');
    assert.equal(JSON.parse(fs.readFileSync(cursorPath, 'utf8')).since, 'S2', '失败批次不得推进游标');
    await waitFor(() => matrixSyncListenerState().healthy === true);
    assert.equal(matrixSyncListenerState().consecutiveFailures, 0, '重试成功后失败计数归零（恢复健康）');
    assert.equal(matrixSyncListenerState().lastError, null);
  } finally { await cleanup(dir); }
});

test('g) trace 脱敏：零 token/URL/密码/正文全文，白名单字段断言', async () => {
  const dir = tmpDir();
  const BODY_SENTINEL = 'SECRETPAYLOAD-XYZ-MUST-NOT-APPEAR';
  const replyBody = `${MARKER} {"verdict":"PASS","note":"${BODY_SENTINEL}"}`;
  let released = false; // 投放门控：waiter 注册后才送达（事件批不重投——不可提前消费）
  const lines = [];
  const orig = console.log;
  console.log = (s) => lines.push(String(s));
  try {
    process.env.AT_SYNC_DIAG = '1';
    const stub = mkSyncFetch({ script: (since) => {
      if (!since) return { batch: { next_batch: 'S1', rooms: { join: {} } } };
      if (since === 'S1' && released) return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(replyBody, { eventId: '$trace-1' })] } } } } } };
      return null;
    } });
    startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    await waitFor(() => stub.state.syncCalls >= 1);
    const w = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 5_000 });
    released = true;
    await w.promise;
  } finally {
    delete process.env.AT_SYNC_DIAG;
    console.log = orig;
    await cleanup(dir);
  }
  const syncLines = lines.filter((l) => l.startsWith('[mt-sync] '));
  assert.ok(syncLines.length >= 2, '应产生 started 与事件 trace 行');
  const blob = lines.join('\n');
  assert.ok(!/access_token/i.test(blob), 'trace 不得包含 token');
  assert.ok(!blob.includes('https://mxsync.test'), 'trace 不得包含完整 URL');
  assert.ok(!blob.includes('sync-pw'), 'trace 不得包含密码');
  assert.ok(!blob.includes(BODY_SENTINEL), 'trace 不得包含消息正文全文');
  assert.ok(!blob.includes('"verdict"'), 'trace 不得包含正文片段');
  const ALLOWED = new Set(['op', 'room', 'sender', 'ts', 'event', 'marker', 'marker_hit', 'outcome',
    'failures', 'kind', 'budget_ms', 'cursor_restored']);
  for (const l of syncLines) {
    const obj = JSON.parse(l.slice('[mt-sync] '.length));
    for (const k of Object.keys(obj)) assert.ok(ALLOWED.has(k), `trace 字段越界: ${k}`);
  }
  const settled = syncLines.map((l) => JSON.parse(l.slice(10))).find((o) => o.outcome === 'settled');
  assert.ok(settled, '应有 settled 事件 trace 行');
  assert.equal(settled.room, ROOM_A);
  assert.equal(settled.sender, SENDER);
  assert.equal(settled.marker_hit, true);
  assert.equal(settled.marker, MARKER, 'marker 本身可输出（无敏感内容）');
  assert.ok(Number(settled.ts) > 0);
});

test('h) 健康标志语义：未启动 false → 启动 true → 连续失败≥3 false → 恢复 true → 停止 false', async () => {
  const dir = tmpDir();
  let failing = true;
  const stub = mkSyncFetch({ script: (since) => (failing
    ? { status: 500 }
    : (!since ? { batch: { next_batch: 'S1', rooms: { join: {} } } } : null)) });
  try {
    __resetMatrixSyncListenerForTests();
    assert.equal(isMatrixSyncListenerHealthy(), false, '未启动=unhealthy');
    const started = startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5, sleepImpl: async () => {} });
    assert.equal(started.ok, true);
    assert.equal(isMatrixSyncListenerHealthy(), true, '刚启动（0 连败）为 healthy');
    await waitFor(() => stub.state.syncCalls >= MSL_LIMITS.failThreshold);
    assert.equal(isMatrixSyncListenerHealthy(), false, '连续失败达阈值=unhealthy');
    assert.ok(matrixSyncListenerState().consecutiveFailures >= MSL_LIMITS.failThreshold);
    assert.ok(!/https?:\/\//.test(String(matrixSyncListenerState().lastError ?? '')), '诊断 lastError 不含 URL');
    failing = false;
    await waitFor(() => isMatrixSyncListenerHealthy() === true);
    assert.equal(matrixSyncListenerState().consecutiveFailures, 0, '成功后失败计数归零');
  } finally { await cleanup(dir); }
  assert.equal(isMatrixSyncListenerHealthy(), false, '停止后=unhealthy');
});

test('i) waiter cancel 立即返回 MSL_CANCELLED；未启动注册防御短路 MSL_NOT_STARTED', async () => {
  __resetMatrixSyncListenerForTests();
  const w0 = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER, sinceTs: 0, timeoutMs: 5_000 });
  const r0 = await w0.promise;
  assert.equal(r0.ok, false);
  assert.equal(r0.reason, 'MSL_NOT_STARTED');
  const dir = tmpDir();
  const stub = mkSyncFetch({ script: (since) => (!since ? { batch: { next_batch: 'S1', rooms: { join: {} } } } : null) });
  try {
    startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    const w = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 60_000 });
    assert.equal(matrixSyncListenerState().pendingWaiters, 1);
    w.cancel();
    const r = await w.promise;
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'MSL_CANCELLED');
    assert.equal(matrixSyncListenerState().pendingWaiters, 0, 'cancel 后 waiter 摘除');
  } finally { await cleanup(dir); }
});

test('j) 部署级入口环境门：缺配置拒绝；重复启动幂等 MSL_ALREADY_STARTED', async () => {
  const r0 = startMatrixSyncListenerFromEnv({});
  assert.equal(r0.ok, false);
  assert.equal(r0.reason, 'MT_NOT_CONFIGURED');
  const dir = tmpDir();
  const stub = mkSyncFetch({ script: (since) => (!since ? { batch: { next_batch: 'S1', rooms: { join: {} } } } : null) });
  try {
    const r1 = startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    assert.equal(r1.ok, true);
    const r2 = startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json') });
    assert.equal(r2.ok, false);
    assert.equal(r2.reason, 'MSL_ALREADY_STARTED', '进程内单例：重复启动幂等拒绝');
  } finally { await cleanup(dir); }
});

test('m) 绑定防御：过期 ts / 错误 sender 的事件不结算（推送路径等价 collectReply 四重绑定）', async () => {
  const dir = tmpDir();
  let phase = 0;
  const stub = mkSyncFetch({ script: (since) => {
    if (!since) return { batch: { next_batch: 'S1', rooms: { join: {} } } };
    if (since === 'S1' && phase >= 1) return { batch: { next_batch: 'S2', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$stale', ts: Date.now() - 60_000 })] } } } } } };
    if (since === 'S2' && phase >= 2) return { batch: { next_batch: 'S3', rooms: { join: { [ROOM_A]: { timeline: { events: [msgEv(REPLY_BODY, { eventId: '$wrong', sender: '@attacker:hs' })] } } } } } };
    return null;
  } });
  try {
    startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5 });
    await waitFor(() => stub.state.syncCalls >= 1);
    const w = registerReplyWaiter({ roomId: ROOM_A, expectedSender: SENDER, marker: MARKER,
      sinceTs: Date.now() - 2_000, timeoutMs: 800 });
    phase = 1;
    await waitFor(() => matrixSyncListenerState().eventsSeen >= 1); // 过期 ts 事件已处理并过滤
    phase = 2;
    await waitFor(() => matrixSyncListenerState().eventsSeen >= 2); // 错 sender 事件已处理并过滤
    const res = await w.promise; // 两条带 marker 事件均不结算 → 至短超时
    assert.equal(res.ok, false);
    assert.equal(res.reason, 'MSL_TIMEOUT', '时间窗外/错误 sender 的带 marker 事件不得结算');
    assert.equal(matrixSyncListenerState().settled, 0);
    assert.ok(matrixSyncListenerState().eventsSeen >= 2, '两条事件均被处理并过滤');
  } finally { await cleanup(dir); }
});

test('l) 非法 payload 计失败退避不 crash；401 清缓存强制重登', async () => {
  // invalidateMatrixLogin 契约直测（401 清缓存——matrix-transport 新增导出，行为零变化）
  mt.__resetMatrixLoginForTests();
  const stubT = mkSyncFetch({});
  await mt.matrixLogin(CFG, stubT);
  assert.equal(mt.__cachedTokenForTests(), 'tok-u');
  mt.invalidateMatrixLogin();
  assert.equal(mt.__cachedTokenForTests(), null, '清除后下次调用强制重登');
  // 循环级：缺 next_batch 的 payload 计为失败（退避续传），401 后重登
  const dir = tmpDir();
  let phase = 0; // 0: 非法 payload；1: 401 一次；2: 正常
  const stub = mkSyncFetch({ script: (since, st) => {
    if (phase === 0) return { status: 200, ok: true, json: async () => ({}) }; // 缺 next_batch
    if (phase === 1 && !st.last401) return { status: 401 };
    return { batch: { next_batch: since ?? 'S1', rooms: { join: {} } } };
  } });
  try {
    startMatrixSyncListener({ cfg: CFG, fetchImpl: stub, cursorPath: path.join(dir, 'c.json'), idleMs: 5,
      sleepImpl: (ms) => sleep(Math.min(ms, 30)) }); // 限频退避（真实 sleep 片段，防失败风暴）
    await waitFor(() => matrixSyncListenerState().consecutiveFailures >= 1);
    assert.equal(matrixSyncListenerState().lastError, 'MSL_SYNC_BAD_PAYLOAD', '错误只留脱敏类别');
    const logins0 = stub.state.logins;
    phase = 1;
    await waitFor(() => stub.state.last401, { timeoutMs: 8_000 });
    await waitFor(() => stub.state.logins > logins0, { timeoutMs: 8_000 });
    assert.ok(stub.state.logins > logins0, '401 清缓存后下一次迭代强制重登');
    phase = 2;
    await waitFor(() => matrixSyncListenerState().consecutiveFailures === 0, { timeoutMs: 8_000 });
    assert.equal(isMatrixSyncListenerHealthy(), true, '恢复后回到 healthy');
  } finally { await cleanup(dir); }
});

test('z) 游标路径解析：env 优先，缺省落系统临时目录', () => {
  assert.equal(resolveSyncCursorPath({ MU_MATRIX_SYNC_CURSOR_PATH: '/x/y.json' }), '/x/y.json');
  const dflt = resolveSyncCursorPath({});
  assert.ok(dflt.endsWith('mp-matrix-sync-cursor.json'), '缺省文件名固定');
  assert.ok(dflt.startsWith(os.tmpdir()), '缺省在系统临时目录下');
});
