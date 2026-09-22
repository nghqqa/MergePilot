// status-map.test.mjs — 状态语义映射测试（提示词九"数据语义"验证矩阵的固化）
// 运行：node --test console/backend/test/status-map.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { executionMap, verdictMap, gateMap, publishMap } from '../../frontend/src/status-map.js';

test('HIGH + 执行完成：执行态是中性事实，不显示成安全通过', () => {
  const exec = executionMap({ status: 'COMPLETED', source: 'project_meta' });
  assert.equal(exec.tone, 'neutral');
  assert.match(exec.note, /不代表无安全问题/);
  assert.doesNotMatch(exec.label, /通过|成功/);

  const processed = executionMap({ status: 'PROCESSED', source: 'delivery_ledger' });
  assert.equal(processed.tone, 'neutral');
  assert.match(processed.note, /不代表审查通过或检查通过/);
});

test('APPROVED + HIGH：批准 ≠ 已修复 ≠ 已合并', () => {
  const gate = gateMap('APPROVED');
  assert.equal(gate.tone, 'info');
  assert.match(gate.note, /不代表问题已修复/);
  assert.match(gate.note, /更不代表已合并/);

  const verdict = verdictMap({
    verdict: 'FINDING_CONFIRMED', severity: 'HIGH', cwe: 'CWE-22', source: 'project/result.md',
  });
  assert.equal(verdict.tone, 'bad');
  assert.match(verdict.label, /HIGH/);
  assert.match(verdict.note, /发现确认不等于已修复/);
});

test('GitHub 成功回写一个 failure check：回写成功与检查未通过是两个事实', () => {
  const p = publishMap({
    status: 'published', conclusion: 'failure', check_run_id: 123,
  });
  assert.equal(p.tone, 'warn', '不是 bad（回写本身成功了）');
  assert.match(p.label, /已回写/);
  assert.match(p.label, /检查未通过/);
  assert.match(p.note, /回写通道成功/);
  assert.match(p.note, /不是回写失败/);
});

test('check success：绿色正向成立，但不证明补丁已验证', () => {
  const p = publishMap({ status: 'published', conclusion: 'success', check_run_id: 5 });
  assert.equal(p.tone, 'ok');
  assert.match(p.note, /不证明补丁已验证/);
});

test('无发布记录：不能断言"未回写"', () => {
  const p = publishMap({ status: 'not_recorded' });
  assert.equal(p.tone, 'neutral');
  assert.match(p.label, /未找到发布记录/);
  assert.match(p.note, /不推导为"未回写"/);
  const p2 = publishMap({ status: 'processed_no_checkrun_record' });
  assert.match(p2.note, /不代表未回写/);
});

test('未记录结论 ≠ 无问题', () => {
  const v = verdictMap({});
  assert.equal(v.tone, 'neutral');
  assert.match(v.note, /不等于"无问题"/);
});

test('LOW/MEDIUM 确认发现用 warn，不用红色', () => {
  assert.equal(verdictMap({ verdict: 'FINDING_CONFIRMED', severity: 'LOW' }).tone, 'warn');
  assert.equal(verdictMap({ verdict: 'FINDING_CONFIRMED', severity: 'MEDIUM' }).tone, 'warn');
  assert.equal(verdictMap({ verdict: 'FINDING_CONFIRMED', severity: 'CRITICAL' }).tone, 'bad');
});

test('BLOCKED 是受控停止（warn），ERROR 是执行错误（bad）', () => {
  assert.equal(executionMap({ status: 'BLOCKED' }).tone, 'warn');
  assert.equal(executionMap({ status: 'ERROR' }).tone, 'bad');
});

test('NOT_CONFIRMED 是明确正向（ok），但 note 说明边界', () => {
  const v = verdictMap({ verdict: 'NOT_CONFIRMED', severity: 'LOW' });
  assert.equal(v.tone, 'ok');
  assert.match(v.note, /不等于绝对无风险/);
});

test('人工确认状态：REJECTED 是受控安全停止（warn）', () => {
  assert.equal(gateMap('REJECTED').tone, 'warn');
  assert.equal(gateMap('NOT_REQUIRED').tone, 'neutral');
  assert.equal(gateMap(null).tone, 'neutral');
  assert.match(gateMap('RECORDED', 'human-gate.md').note, /human-gate\.md/);
});
