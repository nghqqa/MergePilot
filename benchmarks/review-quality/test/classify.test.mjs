// benchmarks/review-quality/test/classify.test.mjs — 五类分类规则单测（mock 驱动，零网络）。
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { classifyFindings, loadRules, CATEGORIES } from '../lib/classify.mjs';

const BENCH_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const rules = loadRules(BENCH_DIR);

const SPEC = {
  case_id: 'CASE-T',
  evaluation_rules: {
    target_path: 'src/q.js',
    defect_marker_patterns: ['i\\s*>\\s*0', '首.{0,6}元素'],
    expected_severity: { value: 'P2', provenance: 'registered' },
    severity_tolerance: 1,
  },
};
const F = (over = {}) => ({
  rule_id: 'LLM-logic_bug', severity: 'P2', path: 'src/q.js', line_start: 3,
  title: '倒序循环条件 i > 0 跳过首元素', remediation: '改为 i >= 0', ...over,
});

test('五类词表封闭且规则文件可加载', () => {
  assert.deepEqual([...CATEGORIES].sort(),
    ['ambiguous_spec', 'duplicate_report', 'false_positive', 'general_suggestion', 'true_defect'].sort());
  assert.equal(rules.schema_version, 'rq-rules-v2');
  assert.ok(rules.rules.length >= 5);
});

test('true_defect：目标文件+命中登记标记（defect 变体）', () => {
  const [c] = classifyFindings([F()], SPEC, rules, { variant: 'defect' });
  assert.equal(c.category, 'true_defect');
  assert.equal(c.rule_id, 'CLA-TRUE-DEFECT');
  assert.equal(c.expected_severity, 'P2');
  assert.equal(c.severity_exact, true);
  assert.equal(c.severity_within_tolerance, true);
});

test('severity_deviation：评 P3 期望 P2 → 偏差 +1，容差内不精确', () => {
  const [c] = classifyFindings([F({ severity: 'P3', title: '缺少 null 防护，首元素未处理 i > 0' })], SPEC, rules, { variant: 'defect' });
  assert.equal(c.category, 'true_defect');
  assert.equal(c.severity_deviation, 1);
  assert.equal(c.severity_exact, false);
  assert.equal(c.severity_within_tolerance, true); // 容差 ±1
});

test('同位置第二条报告（即使也命中标记）→ duplicate_report', () => {
  const cs = classifyFindings(
    [F({ line_start: 5 }), F({ line_start: 4, title: 'double 未返回内部 Promise，首元素循环 i > 0 有误' })],
    SPEC, rules, { variant: 'defect' });
  assert.equal(cs[0].category, 'true_defect');
  assert.equal(cs[1].category, 'duplicate_report');
  assert.equal(cs[1].rule_id, 'CLA-DUPLICATE');
});

test('距离超出窗口的同文件同类别不算重复（按其余规则分类）', () => {
  const cs = classifyFindings(
    [F({ line_start: 2 }), F({ line_start: 40, title: '末尾缺少换行符' })],
    SPEC, rules, { variant: 'defect' });
  assert.equal(cs[0].category, 'true_defect');
  assert.equal(cs[1].category, 'general_suggestion'); // 无缺陷断言 → 兜底
});

test('fixed 变体命中登记标记 → false_positive（断言已修复不存在的缺陷）', () => {
  const [c] = classifyFindings([F()], SPEC, rules, { variant: 'fixed' });
  assert.equal(c.category, 'false_positive');
  assert.equal(c.rule_id, 'CLA-FALSE-POSITIVE');
});

test('建议措辞且无强缺陷断言 → general_suggestion（不冤枉为误报，对齐 trial §六）', () => {
  const [c] = classifyFindings(
    [F({ title: '建议对 items 参数增加数组类型校验', remediation: '考虑 Array.isArray 前置检查' })],
    SPEC, rules, { variant: 'defect' });
  assert.equal(c.category, 'general_suggestion');
  assert.equal(c.rule_id, 'CLA-SUGGESTION');
});

test('目标文件上缺陷断言但标记不符 → false_positive', () => {
  const [c] = classifyFindings(
    [F({ title: '函数未做输入校验会导致崩溃，防护不完整' })],
    SPEC, rules, { variant: 'defect' });
  assert.equal(c.category, 'false_positive');
});

test('diff 外文件的缺陷断言 → false_positive', () => {
  const [c] = classifyFindings(
    [F({ path: 'lib/other.js', title: '该文件存在 SQL 拼接缺陷' })],
    SPEC, rules, { variant: 'defect' });
  assert.equal(c.category, 'false_positive');
});

test('歧义表达（无缺陷断言）→ ambiguous_spec', () => {
  const [c] = classifyFindings(
    [F({ title: '边界是否应该包含 min/max 需要确认，规格不明确' })],
    SPEC, rules, { variant: 'defect' });
  assert.equal(c.category, 'ambiguous_spec');
  assert.equal(c.rule_id, 'CLA-AMBIGUOUS');
});

test('零 finding → 零分类（不虚构）', () => {
  assert.deepEqual(classifyFindings([], SPEC, rules, { variant: 'defect' }), []);
});
