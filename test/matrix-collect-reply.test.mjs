// test/matrix-collect-reply.test.mjs — collectReply 分页/筛选行为回归（CI 门内）。
// 被测对象=真实 matrix-transport.mjs collectReply；fetch 全 stub（合成房间时间线）。
// 覆盖（#519 续波实证要求）：合法回复位于第二页/超过页预算的后续消息/错误 sender/
// 错误 marker/跨轮次旧回复拒绝/无合法回复超时——全部行为断言，非常量文本。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { collectReply } = await import('../console/backend/lib/multiuser/agents/matrix-transport.mjs');

const CFG = { baseUrl: 'https://mx.test', user: 'u', password: 'p' };
const SENDER = '@mergepilot-verifier:mx.test';
const MARKER = '[mp:SUB-R2]';
const LOGIN = JSON.stringify({ user_id: '@admin:mx.test', access_token: 'AT', home_server: 'mx.test' });

// 合成时间线工具：事件按 ts 升序生成；dir=b 返回=逆序分页（每页 20 条）
function timeline(builder) {
  const ev = [];
  const add = (ts, sender, body) => ev.push(msgEv(ts, sender, body));
  builder(add);
  return ev;
}
function msgEv(ts, sender, body) {
  return { type: 'm.room.message', origin_server_ts: ts, sender, content: { body } };
}
function mkFetch(ev, capture) {
  return async (url) => {
    if (capture) capture.push(url);
    if (url.includes('/login')) return { ok: true, json: async () => JSON.parse(LOGIN) };
    if (url.includes('/messages')) {
      const u = new URL(url);
      const limit = Number(u.searchParams.get('limit') || 20);
      const from = u.searchParams.get('from') || '';
      // 时间线按 ts 升序；dir=b 从最新往旧
      const desc = ev.slice().reverse();
      let start = from ? Number(from) : 0;
      const page = desc.slice(start, start + limit);
      const next = start + limit < ev.length ? String(start + limit) : '';
      return { ok: true, json: async () => ({ chunk: page, next_batch: next }) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };
}
const noSleep = async () => {};
const T0 = 1_700_000_000_000;

test('合法回复位于第二页（前 20 条为后续消息）→ 被接受', async () => {
  // 房间时间线：回复在 ts=T0+100；其后 25 条更晚的填充消息 → dir=b 第一页=最新 20 条（无回复）→ 第二页含回复
  const ev = timeline((add) => {
    add(T0 + 100, SENDER, `${MARKER}:t-verify:1] {"verdict":"PASS","note":"ok"}`);
    for (let i = 0; i < 25; i++) add(T0 + 200 + i, '@filler:mx.test', `filler ${i}`);
  });
  const got = await collectReply(CFG, {
    room: '!r:mx.test', expectedSender: SENDER, marker: MARKER, sinceTs: T0,
    timeoutMs: 5_000, fetchImpl: mkFetch(ev), sleepImpl: noSleep, tsNow: () => T0 + 300,
  });
  assert.equal(got.ok, true, '第二页回复必须被接受');
  assert.equal(got.json.verdict, 'PASS');
});

test('超过页预算的后续消息（回复被挤出有界预算）→ 有界超时，不挂起', async () => {
  const ev = timeline((add) => {
    add(T0 + 100, SENDER, `${MARKER}:t-verify:1] {"verdict":"PASS","note":"ok"}`);
    for (let i = 0; i < 200; i++) add(T0 + 200 + i, '@filler:mx.test', `filler ${i}`);
  });
  const t0 = Date.now();
  const got = await collectReply(CFG, {
    room: '!r:mx.test', expectedSender: SENDER, marker: MARKER, sinceTs: T0,
    timeoutMs: 2_000, fetchImpl: mkFetch(ev), sleepImpl: noSleep, tsNow: () => T0 + 300,
  });
  assert.equal(got.ok, false);
  assert.equal(got.reason, 'MT_REPLY_TIMEOUT');
  assert.ok(Date.now() - t0 < 5_000, '必须在预算内返回（不无限翻页）');
});

test('错误 sender 与错误 marker 的带 marker 消息被拒绝 → 继续轮询至超时', async () => {
  const ev = timeline((add) => {
    add(T0 + 100, '@wrong-sender:mx.test', `${MARKER}:t-verify:1] {"verdict":"PASS"}`); // marker 对 sender 错
    add(T0 + 110, SENDER, '[mp:OTHER-SUB:t-verify:1] {"verdict":"PASS"}'); // sender 对 marker 错（不同 submissionId）
  });
  const got = await collectReply(CFG, {
    room: '!r:mx.test', expectedSender: SENDER, marker: MARKER, sinceTs: T0,
    timeoutMs: 1_000, fetchImpl: mkFetch(ev), sleepImpl: noSleep, tsNow: () => T0 + 150,
  });
  assert.equal(got.ok, false);
  assert.equal(got.reason, 'MT_REPLY_TIMEOUT');
});

test('跨轮次隔离：旧轮次回复（旧 submissionId）不得满足新轮次 marker', async () => {
  const ev = timeline((add) => {
    add(T0 + 100, SENDER, '[mp:SUB-R1:t-verify:1] {"verdict":"PASS","note":"old round"}');
    add(T0 + 200, SENDER, `${'[mp:SUB-R2]'}:t-verify:1] {"verdict":"PASS","note":"new round"}`);
  });
  const got = await collectReply(CFG, {
    room: '!r:mx.test', expectedSender: SENDER, marker: '[mp:SUB-R2]', sinceTs: T0,
    timeoutMs: 2_000, fetchImpl: mkFetch(ev), sleepImpl: noSleep, tsNow: () => T0 + 300,
  });
  assert.equal(got.ok, true);
  assert.equal(got.json.note, 'new round', '必须接受新轮次的回复而非旧轮次');
});

test('无合法回复 → MT_REPLY_TIMEOUT（fail-closed 语义保留）', async () => {
  const ev = timeline((add) => {
    add(T0 + 100, SENDER, '处理中...'); // 无 marker
  });
  const got = await collectReply(CFG, {
    room: '!r:mx.test', expectedSender: SENDER, marker: MARKER, sinceTs: T0,
    timeoutMs: 1_000, fetchImpl: mkFetch(ev), sleepImpl: noSleep, tsNow: () => T0 + 300,
  });
  assert.equal(got.ok, false);
  assert.equal(got.reason, 'MT_REPLY_TIMEOUT');
});

test('诊断开关：AT_COLLECT_DIAG=1 输出有界摘要（无 token/无 URL/无正文）', async () => {
  const ev = timeline((add) => {
    add(T0 + 100, SENDER, `${MARKER}:t-verify:1] {"verdict":"PASS"}`);
  });
  const lines = [];
  const orig = console.log;
  console.log = (s) => lines.push(String(s));
  try {
    process.env.AT_COLLECT_DIAG = '1';
    await collectReply(CFG, {
      room: '!r:mx.test', expectedSender: SENDER, marker: MARKER, sinceTs: T0,
      timeoutMs: 2_000, fetchImpl: mkFetch(ev), sleepImpl: noSleep, tsNow: () => T0 + 300,
    });
  } finally {
    delete process.env.AT_COLLECT_DIAG;
    console.log = orig;
  }
  assert.ok(lines.length >= 1, '应有诊断行');
  const blob = lines.join('\n');
  assert.ok(!blob.includes('access_token'), '诊断不得包含 token');
  assert.ok(!blob.includes('https://mx.test'), '诊断不得包含完整 URL');
  assert.ok(!blob.includes('verdict'), '诊断不得包含消息正文');
});
