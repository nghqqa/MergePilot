// api-error-copy.test.mjs — 共享错误文案映射回归（纯函数）。
// 锁定（2026-10-09 D6/D7）：403 授权态不得渲染为"请求失败"；401 引导登录；
// 依赖缺失/成员停用有专属直白文案；5xx/网络为真实故障态。
import test from 'node:test';
import assert from 'node:assert/strict';
import { apiErrorCopy } from '../src/api-error-copy.js';

test('403 action_not_granted → 无权限（不出现"请求失败"字样）', () => {
  const c = apiErrorCopy({ status: 403, message: 'action_not_granted' });
  assert.equal(c.tone, 'warning');
  assert.equal(c.text, '无权限');
  assert.ok(!c.text.includes('请求失败'));
  assert.ok(/没有执行此操作的权限/.test(c.detail));
});

test('403 binding_required → 仓库绑定专属文案（不泛化为平台管理员）', () => {
  const c = apiErrorCopy({ status: 403, message: 'binding_required' });
  assert.equal(c.text, '仓库尚未绑定或绑定已失效');
  assert.ok(/绑定/.test(c.detail));
});

test('401 → 登录引导（非无权限语义）', () => {
  const c = apiErrorCopy({ status: 401, message: 'session_expired' });
  assert.equal(c.tone, 'warning');
  assert.ok(/重新登录/.test(c.detail));
  assert.ok(!c.text.includes('无权限'));
});

test('404 → 不泄露对象存在性', () => {
  const c = apiErrorCopy({ status: 404, message: 'not_found' });
  assert.match(c.text, /不存在或无权访问/);
});

test('409 → 状态冲突引导刷新', () => {
  const c = apiErrorCopy({ status: 409, message: 'approval_state_conflict' });
  assert.equal(c.tone, 'warning');
  assert.ok(/状态/.test(c.text));
});

test('5xx/网络 → 真实故障态', () => {
  assert.equal(apiErrorCopy({ status: 502, message: 'bad_gateway' }).text, '服务暂时不可用');
  assert.equal(apiErrorCopy({ status: 0, message: 'network' }).text, '网络异常');
  assert.equal(apiErrorCopy({}).text, '网络异常');
});

test('未知 4xx 兜底保留机器码（不虚构语义）', () => {
  const c = apiErrorCopy({ status: 422, message: 'cannot_conclude_mergeable' });
  assert.ok(c.text.includes('cannot_conclude_mergeable'));
});
