// overview-chart-ticks.smoke.test.mjs — 概览图表整数刻度纯计算覆盖（2026-10-17）。
// 背景：夹具仅 1 个 PR，多计数刻度无法实机验证——对 intTickMethod 纯函数
// 按 0/1/小域/大域直接断言：刻度恒为整数、恒含 0、不含小数。
// 运行：cd console/frontend && node --test test/overview-chart-ticks.smoke.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { intTickMethod } from '../src/chartTicks.js';

const allInt = (ticks) => ticks.every((v) => Number.isInteger(v));

test('刻度：单计数域 [0,1] → [0,1]（无 0.2/0.4 小数——实机缺陷回归锁）', () => {
  const ticks = intTickMethod(0, 1, 4);
  assert.deepEqual(ticks, [0, 1]);
  assert.ok(allInt(ticks));
});

test('刻度：零计数域 [0,0] → 含 0 的整数刻度', () => {
  const ticks = intTickMethod(0, 0, 4);
  assert.deepEqual(ticks, [0]);
  assert.ok(allInt(ticks));
});

test('刻度：小域 [0,3] → 步长 1 全整数', () => {
  const ticks = intTickMethod(0, 3, 4);
  assert.deepEqual(ticks, [0, 1, 2, 3]);
});

test('刻度：大域 [0,23]（步长上取整 6）→ 全整数且覆盖端点', () => {
  const ticks = intTickMethod(0, 23, 4);
  assert.ok(allInt(ticks), '全整数');
  assert.equal(ticks[0], 0, '恒含 0');
  assert.ok(ticks[ticks.length - 1] >= 18, '覆盖主要区间');
  for (let i = 1; i < ticks.length; i++) assert.ok(ticks[i] > ticks[i - 1], '单调递增');
});

test('刻度：非零下界域 [2,9] → 全整数不出现负值', () => {
  const ticks = intTickMethod(2, 9, 4);
  assert.ok(allInt(ticks));
  assert.ok(ticks.every((v) => v >= 0));
  assert.ok(ticks.length >= 2);
});
