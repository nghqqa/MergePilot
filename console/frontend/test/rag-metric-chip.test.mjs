// rag-metric-chip.test.mjs — RAG 指标徽章诚实呈现（rc8.2 修复回归）。
// 缺陷史：错误态（provider_unavailable 等）按全期累计计数渲染红色徽章，
// 三周前的一次历史失败被呈现得像"当前故障"（2026-10-25 生产实录）。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(__dirname, '..');
const mod = await import(pathToFileURL(path.join(FRONTEND, 'src', 'rag-metric-chip.js')).href);

test('错误态且此后已有成功检索（整体最新晚于错误最新）→ 历史中性呈现+最近时间', () => {
  const chip = mod.metricChipFor('provider_unavailable', 1,
    '2026-10-03T20:44:52+08:00', '2026-10-25T10:58:24+08:00');
  assert.equal(chip.tone, 'neutral', ' recovered 必须去警示色');
  assert.ok(chip.text.includes('历史'), chip.text);
  assert.ok(chip.text.includes('此后检索正常'), chip.text);
  assert.ok(chip.text.includes('最近'), chip.text);
  assert.ok(chip.text.includes('× 1'), chip.text);
});

test('错误态且仍为最新事件（无更晚成功检索）→ 保持警示色+最近发生时间', () => {
  const chip = mod.metricChipFor('provider_unavailable', 2,
    '2026-10-25T10:58:24+08:00', '2026-10-25T10:58:24+08:00');
  assert.equal(chip.tone, 'bad', '当前不可用必须保持警示色');
  assert.ok(!chip.text.includes('历史'), chip.text);
  assert.ok(chip.text.includes('最近'), chip.text);
});

test('无时间数据 → 回落现行警示呈现（不臆造 recovered）', () => {
  const chip = mod.metricChipFor('provider_unavailable', 1, null, null);
  assert.equal(chip.tone, 'bad');
  assert.ok(!chip.text.includes('历史'));
});

test('非错误态原样透传（hit/empty 文案与色不变）', () => {
  const hit = mod.metricChipFor('hit', 142, null, null);
  assert.equal(hit.tone, 'ok');
  assert.equal(hit.text, '命中 × 142');
  const empty = mod.metricChipFor('empty', 11, null, null);
  assert.equal(empty.tone, 'neutral');
  assert.equal(empty.text, '无结果 × 11');
});

test('接线契约：页面走 metricChipFor；store metrics 暴露最近时间字段', () => {
  const page = fs.readFileSync(path.join(FRONTEND, 'src', 'pages', 'RagTrialPage.jsx'), 'utf8');
  assert.ok(page.includes('metricChipFor(s, n, metrics.queries_latest_by_state?.[s], metrics.latest_overall)'),
    '页面必须用 helper 并传入最近时间');
  const store = fs.readFileSync(path.join(FRONTEND, '..', 'backend', 'lib', 'ragtrial', 'store.mjs'), 'utf8');
  assert.ok(store.includes('queries_latest_by_state'), 'store 必须暴露各状态最近发生时间');
  assert.ok(store.includes('latest_overall'), 'store 必须暴露整体最近事件');
  // 回归（rc8.3）：metrics() 体内必须先定义后引用 latestByState——上一次逐行补丁
  // 断言中断导致定义缺失、metrics 500、页面误报「服务暂不可用」。
  const mStart = store.indexOf('async function metrics');
  const mBody = store.slice(mStart, mStart + 2600);
  const defAt = mBody.indexOf('const latestByState = await q(');
  const useAt = mBody.indexOf('latestByState.rows');
  assert.ok(defAt > -1 && useAt > defAt, 'metrics() 必须定义 latestByState 且先于引用');
  assert.ok(mBody.includes('const latestOverall = await q('), 'latestOverall 定义必须在场');
});
