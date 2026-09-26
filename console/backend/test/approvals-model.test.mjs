// approvals-model.test.mjs — 审批交互状态机验证（批次 B 验收矩阵固化）
// 运行：node --test console/backend/test/approvals-model.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { validateDecision, submissionReducer } from '../../frontend/src/approvals-model.js';

const NOW = Date.parse('2026-09-22T12:00:00Z');
const ticket = (over = {}) => ({
  id: 'T1', status: 'PENDING', head_sha: 'a'.repeat(40),
  expires_at: '2026-12-31T00:00:00Z', ...over,
});

test('提交前预检：非 PENDING / 非法决策 / 过期 / head 冲突 分别拒绝', () => {
  assert.equal(validateDecision(ticket({ status: 'USED' }), { decision: 'APPROVED', now: NOW }).code, 'NOT_PENDING');
  assert.equal(validateDecision(ticket(), { decision: 'MAYBE', now: NOW }).code, 'BAD_DECISION');
  assert.equal(
    validateDecision(ticket({ expires_at: '2026-09-01T00:00:00Z' }), { decision: 'APPROVED', now: NOW }).code,
    'EXPIRED'
  );
  assert.equal(
    validateDecision(ticket(), { decision: 'APPROVED', now: NOW, assumeCurrentHead: 'b'.repeat(40) }).code,
    'HEAD_CONFLICT'
  );
  assert.equal(validateDecision(ticket(), { decision: 'APPROVED', now: NOW }).ok, true);
  // snapshot 无当前 head 权威时不做该项预检（不得假装知道当前 head）
  assert.equal(validateDecision(ticket(), { decision: 'APPROVED', now: NOW, assumeCurrentHead: null }).ok, true);
});

test('状态机：仅 idle 可发起提交（防重复点击），submitting 中再点被忽略', () => {
  const s1 = submissionReducer({ phase: 'idle' }, { type: 'SUBMIT', decision: 'APPROVED' });
  assert.equal(s1.phase, 'submitting');
  const s2 = submissionReducer(s1, { type: 'SUBMIT', decision: 'REJECTED' });
  assert.equal(s2.phase, 'submitting', 'submitting 中重复提交被忽略');
  assert.equal(s2.decision, 'APPROVED', '保留首次决策');
});

test('状态机：超时 → 结果未知 → 必须查询，不得盲目重发', () => {
  let s = submissionReducer({ phase: 'idle' }, { type: 'SUBMIT', decision: 'APPROVED' });
  s = submissionReducer(s, { type: 'TIMEOUT' });
  assert.equal(s.phase, 'unknown');
  const ignored = submissionReducer(s, { type: 'SUBMIT', decision: 'APPROVED' });
  assert.equal(ignored.phase, 'unknown', 'unknown 状态禁止直接重发');
  s = submissionReducer(s, { type: 'QUERY', outcome: 'approved' });
  assert.equal(s.phase, 'decided');
  assert.equal(s.outcome, 'approved');
  assert.equal(s.via, 'query', '结果经由查询获得，非乐观更新');
});

test('状态机：冲突与过期是终态，刷新后回到 idle', () => {
  let s = submissionReducer({ phase: 'idle' }, { type: 'SUBMIT', decision: 'REJECTED' });
  s = submissionReducer(s, { type: 'CONFLICT' });
  assert.equal(s.phase, 'conflict');
  assert.equal(submissionReducer(s, { type: 'RESOLVE', outcome: 'rejected' }).phase, 'conflict', '终态不再迁移');
  s = submissionReducer(s, { type: 'REFRESH' });
  assert.equal(s.phase, 'idle');

  s = submissionReducer({ phase: 'idle' }, { type: 'SUBMIT', decision: 'APPROVED' });
  s = submissionReducer(s, { type: 'EXPIRE' });
  assert.equal(s.phase, 'expired');
});

test('状态机：RESOLVE 仅在 submitting 中生效（不乐观更新 UI）', () => {
  const idle = submissionReducer({ phase: 'idle' }, { type: 'RESOLVE', outcome: 'approved' });
  assert.equal(idle.phase, 'idle', '未提交就收到结果 = 忽略');
  let s = submissionReducer({ phase: 'idle' }, { type: 'SUBMIT', decision: 'APPROVED' });
  s = submissionReducer(s, { type: 'RESOLVE', outcome: 'rejected' });
  assert.equal(s.phase, 'decided');
  assert.equal(s.outcome, 'rejected');
});
