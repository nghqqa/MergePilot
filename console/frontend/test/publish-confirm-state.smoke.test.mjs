// publish-confirm-state.smoke.test.mjs — 发布评论确认门纯函数覆盖（2026-10-18 契约收口）。
// 背景：后端 publishRun 在远端 head 读取失败/不一致时严格拒写（先于任何远端写调用），
// 但前端抽屉「确认发布」此前不随远端状态禁用——文案「确认保持阻塞」与按钮可点矛盾。
// 本测试锁 publishConfirmState 的四态判定；组件侧由 Drawer disabled + Tooltip 消费。
// 运行：cd console/frontend && node --test test/publish-confirm-state.smoke.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { publishConfirmState } from '../src/publishConfirmState.js';

test('match：远端 head 已核实一致 → 不禁用（唯一放行态）', () => {
  const s = publishConfirmState({ remote_head: 'a'.repeat(40), head_match: true });
  assert.equal(s.blocked, false);
  assert.equal(s.reason, null);
});

test('unreadable：远端读取失败（如 No key provided to sign）→ 禁用且原因含原始错误', () => {
  const s = publishConfirmState({ remote_head: null, head_match: null, remote_head_error: 'No key provided to sign' });
  assert.equal(s.blocked, true);
  assert.match(s.reason, /无法读取/);
  assert.match(s.reason, /No key provided to sign/);
  assert.match(s.reason, /重新预览/);
});

test('unknown：未取得远端 head（远端不可达或未返回）→ 禁用', () => {
  const s = publishConfirmState({ remote_head: null, head_match: null });
  assert.equal(s.blocked, true);
  assert.match(s.reason, /阻塞/);
});

test('changed：远端 head 已核实不同 → 禁用（服务端本就 409，前端如实呈现）', () => {
  const s = publishConfirmState({ remote_head: 'b'.repeat(40), head_match: false });
  assert.equal(s.blocked, true);
  assert.match(s.reason, /已变化/);
  assert.match(s.reason, /409/);
});

test('空上下文（pubCtx 未就绪）→ 默认禁用（fail-closed）', () => {
  const s = publishConfirmState(null);
  assert.equal(s.blocked, true);
});
