// console/backend/test/r4-trend.test.mjs — trend 当日分桶修复回归（FB-05）。
// isoDayOf：Date（pg 驱动 timestamptz 返回）/ ISO 字符串 / 非法值 → UTC 日期桶。
// 运行：node --test console/backend/test/r4-trend.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isoDayOf } from '../lib/core-pilot.mjs';

test('isoDayOf: Date 对象 → UTC YYYY-MM-DD（不再产出 "Wed Sep 25" 类串）', () => {
  const d = new Date('2026-09-25T08:12:52.000Z');
  assert.strictEqual(isoDayOf(d), '2026-09-25');
  const d2 = new Date('2026-09-25T23:59:59.000Z');
  assert.strictEqual(isoDayOf(d2), '2026-09-25');
});

test('isoDayOf: ISO 字符串取前 10 位；非 ISO 字符串拒绝', () => {
  assert.strictEqual(isoDayOf('2026-09-25T08:12:52+00:00'), '2026-09-25');
  assert.strictEqual(isoDayOf('Wed Sep 25 2026'), null);
  assert.strictEqual(isoDayOf(''), null);
  assert.strictEqual(isoDayOf(null), null);
  assert.strictEqual(isoDayOf(undefined), null);
});

test('isoDayOf: 日期桶与 14 天趋势骨架的 UTC 日期一致（当日不再归零）', () => {
  const now = new Date('2026-09-25T08:12:52.000Z');
  const skeleton = [];
  for (let i = 13; i >= 0; i--) {
    skeleton.push({ date: new Date(now.getTime() - i * 86400000).toISOString().slice(0, 10), runs: 0 });
  }
  const latest = isoDayOf(now);
  const hit = skeleton.find((t) => t.date === latest);
  assert.ok(hit, '当日桶必须存在');
  hit.runs += 4;
  const today = skeleton[skeleton.length - 1];
  assert.strictEqual(today.runs, 4);
});
