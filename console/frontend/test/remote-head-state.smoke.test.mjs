// remote-head-state.smoke.test.mjs — 远端 head 三态归一纯计算覆盖（2026-10-17 修复回归）。
// 锁定的语义：远端不可达/缺 head/键名错位 ⇒ 「无法核实」；只有可信远端 head 才判一致/已变化。
// 运行：cd console/frontend && node --test test/remote-head-state.smoke.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { remoteHeadState } from '../src/remoteHeadState.js';
import { previewFailureCopy } from '../src/previewFailureCopy.js';

test('三态：远端可达且 head 一致 → match（含 head）', () => {
  const s = remoteHeadState({ remote_head: 'a'.repeat(40), head_match: true });
  assert.equal(s.key, 'match');
  assert.equal(s.head, 'a'.repeat(40));
});

test('三态：远端可达且 head 不同 → changed（有可信证据才能说「已变化」）', () => {
  const s = remoteHeadState({ remote_head: 'b'.repeat(40), head_match: false });
  assert.equal(s.key, 'changed');
  assert.equal(s.head, 'b'.repeat(40));
});

test('三态：trial1 实测形态——remote_error 键 + 无 head → unreadable（不再误标「已变化」）', () => {
  const s = remoteHeadState({ remote_head: null, head_match: false, remote_error: 'No key provided to sign' });
  assert.equal(s.key, 'unreadable');
  assert.match(s.reason, /No key provided to sign/);
});

test('三态：规范键 remote_head_error 优先（键名错位兼容）', () => {
  const s = remoteHeadState({ remote_head: null, head_match: false, remote_head_error: 'ghp_pr_http_404', remote_error: 'ignored' });
  assert.equal(s.key, 'unreadable');
  assert.match(s.reason, /ghp_pr_http_404/);
});

test('三态：remote_head 空串（成功响应缺 head）→ unknown 而非 changed', () => {
  const s = remoteHeadState({ remote_head: '', head_match: false });
  assert.equal(s.key, 'unknown');
  assert.match(s.reason, /未能取得远端 head/);
});

test('三态：字段全缺/undefined 输入 → unknown（不把缺字段当 false）', () => {
  assert.equal(remoteHeadState({}).key, 'unknown');
  assert.equal(remoteHeadState(undefined).key, 'unknown');
  assert.equal(remoteHeadState({ remote_head: null, head_match: null }).key, 'unknown');
});

test('三态：head 已取得但 head_match 非布尔 → unknown（一致性未判定）', () => {
  const s = remoteHeadState({ remote_head: 'c'.repeat(40), head_match: null });
  assert.equal(s.key, 'unknown');
  assert.match(s.reason, /一致性未判定/);
});

test('预览失败文案：remote_unreachable → 中文原因+下一步+技术详情保留错误码', () => {
  const c = previewFailureCopy({ status: 409, reason: 'remote_unreachable', detail: 'No key provided to sign', what: '合并预览' });
  assert.match(c.lead, /无法读取远端 PR 信息/);
  assert.match(c.lead, /确认保持阻塞/);
  assert.match(c.next, /检查连接器/);
  assert.match(c.tech, /HTTP 409/);
  assert.match(c.tech, /No key provided to sign/);
  // 不得把远端不可达解释为冲突/CI/head 变化
  assert.ok(!/冲突|CI|已变化/.test(c.lead + c.next));
});

test('预览失败文案：其他 reason → 原样保留（不编造解释）', () => {
  const c = previewFailureCopy({ status: 500, reason: 'internal_error', detail: null, what: '应用修复' });
  assert.match(c.lead, /HTTP 500/);
  assert.match(c.lead, /internal_error/);
  assert.equal(c.tech, null);
});
