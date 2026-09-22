// console/backend/test/runs.test.mjs — run 记录归一化测试（fixture + 真实证据根）

import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { buildRunRecord, buildRunDetail } from '../lib/runs.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(__dirname, 'fixtures');
const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const REAL_EVIDENCE = path.join(REPO_ROOT, 'evidence');

const SHA40 = /^[0-9a-f]{40}$/;

test('WH 轮 fixture：身份字段从 ledger/result/check-run 正确归一', () => {
  const r = buildRunRecord('SAMPLE-RUN-WH', path.join(FIXTURES, 'SAMPLE-RUN-WH'));
  assert.equal(r.run_id, 'run-gh-pr9-aabbcc-093022');
  assert.equal(r.repo, 'nghqqa/fastapi-boilerplate-demo');
  assert.equal(r.pr_number, 9);
  assert.equal(r.pr_url, 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/9');
  assert.match(r.head_sha, SHA40);
  assert.match(r.base_sha, SHA40);
  assert.equal(r.trigger, 'webhook');
  assert.equal(r.execution.source, 'delivery_ledger');
  assert.equal(r.execution.status, 'PROCESSED');
  assert.equal(r.publish.status, 'published');
  assert.equal(r.publish.check_run_id, 105876323522);
  assert.equal(r.review.verdict, 'NOT_CONFIRMED');
  assert.equal(r.review.severity, 'LOW');
  assert.equal(r.review.human_gate, 'NOT_REQUIRED');
  assert.ok(r.created_at);
  assert.ok(r.duration_ms > 0);
  assert.equal(r.has_sums, true);
});

test('WH 轮 fixture：执行/审查/发布三态独立存在', () => {
  const r = buildRunRecord('SAMPLE-RUN-WH', path.join(FIXTURES, 'SAMPLE-RUN-WH'));
  assert.ok(r.execution && r.review && r.publish, '三个状态对象必须分别存在');
  // 执行状态词与发布状态词来自不同字段，不能合并为一个"成功"
  assert.notEqual(r.execution.status, r.publish.conclusion);
});

test('Matrix 轮 fixture：无 ledger → trigger=matrix、执行状态来自 project meta、发布未记录', () => {
  const r = buildRunRecord('SAMPLE-RUN-MATRIX', path.join(FIXTURES, 'SAMPLE-RUN-MATRIX'));
  assert.equal(r.run_id, 'run-elem-fixture-matrix-01');
  assert.equal(r.trigger, 'matrix');
  assert.equal(r.execution.source, 'project_meta');
  assert.equal(r.execution.status, 'blocked');
  assert.equal(r.publish.status, 'not_recorded');
  assert.equal(r.repo, null);
  assert.equal(r.head_sha, null);
});

test('WH 轮 fixture：detail 装配（timeline/tasks/evidence 归属）', () => {
  const d = buildRunDetail('SAMPLE-RUN-WH', path.join(FIXTURES, 'SAMPLE-RUN-WH'));
  assert.equal(d.tasks.length, 1);
  assert.equal(d.tasks[0].role, 'reviewer');
  assert.equal(d.tasks[0].result_path, 'tasks/gh-pr9-aabbcc-review-1/result.md');
  assert.ok(d.timeline.length >= 6, 'ledger 3 + task 3 + check-run 2');
  const labels = d.timeline.map((t) => t.label);
  assert.ok(labels.includes('webhook 投递接收'));
  assert.ok(labels.includes('投递处理完成'));
  assert.ok(labels.some((l) => l.includes('check-run')));
  assert.ok(d.dag.length === 3);
  assert.equal(d.rag.state, 'insufficient_data');
  assert.equal(d.usage, null);
  assert.equal(d.versions.run_manifest, null);
});

// ---- 真实证据根回归（工作树含 evidence/ 时执行） ----

const hasReal = fs.existsSync(path.join(REAL_EVIDENCE, 'FINALS-ELEM-PR1-WH-20260919'));

test('真实证据根：WH 轮关键字段实测', { skip: !hasReal }, () => {
  const r = buildRunRecord('FINALS-ELEM-PR1-WH-20260919', path.join(REAL_EVIDENCE, 'FINALS-ELEM-PR1-WH-20260919'));
  assert.equal(r.run_id, 'run-gh-pr1-575aa8e1-093022');
  assert.equal(r.repo, 'nghqqa/fastapi-boilerplate-demo');
  assert.equal(r.pr_number, 1);
  assert.equal(r.head_sha, '575aa8e13146998da65a8f62816740dbb5e539ca');
  assert.equal(r.base_sha, 'fdde4f4142606336c7b7b25f176949dc5882d89a');
  assert.equal(r.execution.status, 'PROCESSED');
  assert.equal(r.publish.status, 'published');
  assert.equal(r.review.verdict, 'NOT_CONFIRMED');
});

test('真实证据根：WH 拒绝案例（PR3，无 project/result.md）走任务级回退', { skip: !hasReal }, () => {
  const r = buildRunRecord('FINALS-ELEM-PR3-WH-20260919', path.join(REAL_EVIDENCE, 'FINALS-ELEM-PR3-WH-20260919'));
  assert.equal(r.run_id, 'run-gh-pr3-03312d65-091817');
  assert.equal(r.head_sha, '03312d6596f0b894a3324e958461a5fc336f2ff5');
  assert.equal(r.review.verdict, 'FINDING_CONFIRMED', 'reviewer 任务 result.md 提供结论');
  assert.equal(r.review.severity, 'HIGH');
  assert.equal(r.review.cwe, 'CWE-78');
  assert.equal(r.review.human_gate, 'REJECTED', 'human-gate-rejection.md → REJECTED');
  assert.equal(r.review.source, 'tasks/gh-pr3-03312d65-review-1/result.md');
});

test('真实证据根：WH 批准案例（PR2）门决策 APPROVED', { skip: !hasReal }, () => {
  const r = buildRunRecord('FINALS-ELEM-PR2-WH-20260919', path.join(REAL_EVIDENCE, 'FINALS-ELEM-PR2-WH-20260919'));
  assert.equal(r.review.verdict, 'FINDING_CONFIRMED');
  assert.equal(r.review.human_gate, 'APPROVED');
});

test('真实证据根：SK5 轮（matrix 触发、含 RAG/usage）', { skip: !hasReal }, () => {
  const d = buildRunDetail('FINALS-ELEM-PR2-SK5-TRACED', path.join(REAL_EVIDENCE, 'FINALS-ELEM-PR2-SK5-TRACED'));
  assert.equal(d.run_id, 'run-elem-pr2sk5-20260919-01');
  assert.equal(d.trigger, 'matrix');
  assert.equal(d.head_sha, '1dedf5e1992c950557064d8f4fb9039d1523deb3');
  assert.equal(d.review.verdict, 'FINDING_CONFIRMED');
  assert.equal(d.review.severity, 'HIGH');
  assert.equal(d.review.human_gate, 'APPROVED');
  assert.equal(d.publish.status, 'not_recorded');
  assert.equal(d.rag.state, 'called');
  assert.ok(d.rag.calls.length >= 5, 'SK5 PR2 rag spans ≥5');
  assert.ok(d.usage, 'SK5 pack has usage-summary.json');
  assert.equal(d.usage.matched_window?.key, 'pr2sk5');
  assert.equal(d.usage.matched_window?.calls, 86);
  assert.ok(d.versions.model, 'model extracted from usage note');
});

test('真实证据根：结论绑定 commit（SK5 PR2 与 PR3 head 不同、结论归属各自 SHA）', { skip: !hasReal }, () => {
  const r2 = buildRunRecord('FINALS-ELEM-PR2-SK5-TRACED', path.join(REAL_EVIDENCE, 'FINALS-ELEM-PR2-SK5-TRACED'));
  const r3 = buildRunRecord('FINALS-ELEM-PR3-SK5-TRACED', path.join(REAL_EVIDENCE, 'FINALS-ELEM-PR3-SK5-TRACED'));
  assert.notEqual(r2.head_sha, r3.head_sha);
  assert.notEqual(r2.run_id, r3.run_id);
});

test('真实证据根：非 run 包（实验包）不被索引', { skip: !hasReal }, () => {
  // DUAL-REVIEWER-EXP 无 run 锚点 → buildRunDetail 应 404（由 server 层保证）；
  // 这里验证它不满足锚点规则本身
  const anchors = ['delivery-ledger.json', 'kickoff.json', 'project/meta.json', 'PR-METADATA.md'];
  const dir = path.join(REAL_EVIDENCE, 'DUAL-REVIEWER-EXP-20260919');
  assert.ok(anchors.every((a) => !fs.existsSync(path.join(dir, a))));
});
