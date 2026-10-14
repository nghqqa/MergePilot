// llm-zero-finding.test.mjs — 零发现回退与诚实 mock（模型审查解耦波）。
// 场景：静态规则零发现时 LLM 审查仍须拿到真实 diff 上下文（脱敏+截断），
// 而不是空 hunks 导致审查空转；mock reviewer 零输入不伪造建议。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..', '..');
const llm = await import(pathToFileURL(path.join(ROOT, 'console', 'backend', 'lib', 'multiuser', 'agents', 'llm.mjs')));

const DIFF = [
  'diff --git a/src/loop.js b/src/loop.js',
  '--- a/src/loop.js',
  '+++ b/src/loop.js',
  '@@ -1,4 +1,4 @@',
  ' function sumDiscounts(prices) {',
  "-for (let i = 0; i <= prices.length; i++) sum += prices[i];",
  "+for (let i = 0; i < prices.length; i++) sum += prices[i];",
  ' }',
  'diff --git a/src/token.js b/src/token.js',
  '+++ b/src/token.js',
  '@@ -1,2 +1,2 @@',
  '-const token = "ghp_abcdefABCDEF1234567890abcdefABCDEF";',
  '+const token = process.env.GITHUB_TOKEN;',
].join('\n');

test('Z1 零发现+有 diff → 回退有限全量 diff（脱敏）', () => {
  const { payload } = llm.sanitizeContextForLlm({ pr: { number: 2, title: 't' }, findings: [], diff: DIFF });
  const hunks = payload.untrusted_data.diff_hunks;
  assert.equal(hunks.length, 1, '单一回退块');
  assert.equal(hunks[0].path, '(full_diff_truncated)');
  const joined = hunks[0].lines.join('\n');
  assert.ok(joined.includes('i <= prices.length'), '含缺陷行（模型可发现逻辑缺陷）');
  assert.ok(joined.includes('i < prices.length'), '含修复后形态');
  assert.ok(!/ghp_[A-Za-z0-9]{8,}/.test(joined), 'token 行已脱敏');
});

test('Z2 超 400 行 diff 截断到上限', () => {
  const long = Array.from({ length: 900 }, (_, i) => `+line ${i}`).join('\n');
  const { payload } = llm.sanitizeContextForLlm({ pr: { number: 1, title: 't' }, findings: [], diff: long });
  assert.ok(payload.untrusted_data.diff_hunks[0].lines.length <= 400, '行数截断');
});

test('Z3 零发现+零 diff → 空 hunks（如实，不伪造）', () => {
  const { payload } = llm.sanitizeContextForLlm({ pr: { number: 1, title: 't' }, findings: [], diff: '' });
  assert.equal(payload.untrusted_data.diff_hunks.length, 0);
});

test('Z4 有 findings → 既有锚点 hunk 行为不变（回归）', () => {
  const findings = [{ path: 'src/token.js', line_start: 1, rule_id: 'R-SECRET', severity: 'P0',
    title: 't', summary_masked: 's' }];
  const { payload } = llm.sanitizeContextForLlm({ pr: { number: 1, title: 't' }, findings, diff: DIFF });
  const hunks = payload.untrusted_data.diff_hunks;
  assert.equal(hunks.length, 1);
  assert.equal(hunks[0].path, 'src/token.js', '按 finding 锚点选文件');
  assert.equal(payload.untrusted_data.deterministic_findings.length, 1);
});

test('Z5 mock reviewer：零输入→零建议（不伪造）', async () => {
  const r = await llm.mockLlmReviewer({ findings: [] });
  assert.equal(r.ok, true);
  assert.equal(r.findings.length, 0, '零输入零建议');
});

test('Z6 mock reviewer：有输入→1 条建议（管线冒烟）', async () => {
  const r = await llm.mockLlmReviewer({ findings: [{ path: 'a.js', line_start: 3 }] });
  assert.equal(r.findings.length, 1);
  assert.equal(r.findings[0].location.path, 'a.js');
});

test('Z7 超预算：按块边界裁剪，代码不归零', () => {
  // 300 块×每块多行——必然超 24KB；断言：至少保留 1 块且有行（非空 hunks）
  const parts = ['diff --git a/src/x0.js b/src/x0.js', '--- a/src/x0.js', '+++ b/src/x0.js', '@@ -1,3 +1,3 @@'];
  for (let b = 0; b < 300; b++) {
    parts.push(`diff --git a/src/f${b}.js b/src/f${b}.js`, `--- a/src/f${b}.js`, `+++ b/src/f${b}.js`, `@@ -1,2 +1,2 @@`,
      `+console.log('block ${b} line A');`, `+console.log('block ${b} line B');`);
  }
  const { payload, coverage } = llm.sanitizeContextForLlm({ pr: { number: 1, title: 't' }, findings: [], diff: parts.join('\n') });
  const hunks = payload.untrusted_data.diff_hunks;
  assert.ok(hunks.length >= 1, '至少保留一块（代码不归零）');
  assert.ok(hunks[0].lines.length > 0, '保留块有行');
  assert.ok(coverage.bytes <= 24 * 1024 + 64, '预算内（含 JSON 包装容差）');
  assert.equal(typeof coverage.truncated, 'boolean');
});

test('Z8 coverage 结构：full_diff_fallback/lines_sent/truncated 可读', () => {
  const { coverage } = llm.sanitizeContextForLlm({ pr: { number: 1, title: 't' }, findings: [], diff: DIFF });
  assert.equal(coverage.full_diff_fallback, true);
  assert.ok(coverage.lines_sent > 0 && coverage.bytes > 0);
  assert.equal(coverage.truncated, false);
});
