// benchmarks/review-quality/test/metrics.test.mjs — 指标计算单测（显式分子/分母 + mock/real 分列）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { computeMetrics, computeMetricSet } from '../lib/metrics.mjs';

const run = (over = {}) => ({
  case_id: 'CASE-T', variant: 'defect', llm_mode: 'real', call_ok: true, call_code: null,
  input_coverage: { target_file_covered: true, coverage: { blocks_sent: 1, lines_sent: 8, truncated: false } },
  classifications: [], ...over,
});
const cls = (category, extra = {}) => ({ category, ...extra });

test('调用完成率：ok/total，失败 run 逐条列出', () => {
  const m = computeMetricSet([
    run(), run({ case_id: 'B', call_ok: false, call_code: 'LLM_TIMEOUT' }),
  ]);
  assert.equal(m.call_completion.numerator, 1);
  assert.equal(m.call_completion.denominator, 2);
  assert.deepEqual(m.call_completion.failed,
    [{ case_id: 'B', variant: 'defect', code: 'LLM_TIMEOUT' }]);
});

test('目标缺陷命中率：分母只含调用成功且可评定的缺陷版 run', () => {
  const m = computeMetricSet([
    run({ case_id: 'C1', classifications: [cls('true_defect')] }),
    run({ case_id: 'C2', classifications: [cls('general_suggestion')] }), // 漏报
    run({ case_id: 'C3', call_ok: false, call_code: 'LLM_HTTP_500' }),   // 不进分母
  ]);
  assert.equal(m.target_hit_rate.numerator, 1);
  assert.equal(m.target_hit_rate.denominator, 2);
  assert.deepEqual(m.target_hit_rate.hit_cases, ['C1']);
  assert.deepEqual(m.target_hit_rate.misses,
    [{ case_id: 'C2', failure_class: 'covered_but_missed' }]);
  assert.deepEqual(m.target_hit_rate.excluded_call_failed, ['C3']);
});

test('漏报分档：目标未入载荷 = target_not_covered（输入问题，非模型漏报）', () => {
  const m = computeMetricSet([
    run({ case_id: 'C9', input_coverage: { target_file_covered: false }, classifications: [] }),
  ]);
  assert.deepEqual(m.target_hit_rate.misses,
    [{ case_id: 'C9', failure_class: 'target_not_covered' }]);
});

test('正确版误报数：只统计 fixed 变体的 false_positive 条数', () => {
  const m = computeMetricSet([
    run({ case_id: 'C1', variant: 'fixed', classifications: [cls('false_positive'), cls('general_suggestion')] }),
    run({ case_id: 'C2', variant: 'fixed', classifications: [] }),
    run({ case_id: 'C3', variant: 'defect', classifications: [cls('false_positive')] }), // defect 变体的误报不进此指标
  ]);
  assert.equal(m.clean_false_positives.numerator, 1);
  assert.equal(m.clean_false_positives.denominator, 2);
});

test('precision 分母 = true_defect + false_positive；其余三类单列不混入', () => {
  const m = computeMetricSet([
    run({ classifications: [cls('true_defect'), cls('duplicate_report'), cls('true_defect')] }),
    run({ case_id: 'B', classifications: [cls('false_positive'), cls('ambiguous_spec'), cls('general_suggestion')] }),
  ]);
  assert.equal(m.precision.numerator, 2);
  assert.equal(m.precision.denominator, 3);
  assert.equal(m.precision.class_counts.duplicate_report, 1);
  assert.equal(m.precision.class_counts.ambiguous_spec, 1);
  assert.equal(m.precision.class_counts.general_suggestion, 1);
});

test('严重度一致性：精确/容差/超容差分列', () => {
  const m = computeMetricSet([
    run({ case_id: 'C1', classifications: [cls('true_defect', { expected_severity: 'P2', rated_severity: 'P2', severity_deviation: 0, severity_exact: true, severity_within_tolerance: true })] }),
    run({ case_id: 'C2', classifications: [cls('true_defect', { expected_severity: 'P2', rated_severity: 'P3', severity_deviation: 1, severity_exact: false, severity_within_tolerance: true })] }),
    run({ case_id: 'C3', classifications: [cls('true_defect', { expected_severity: 'P1', rated_severity: 'P3', severity_deviation: 2, severity_exact: false, severity_within_tolerance: false })] }),
  ]);
  assert.equal(m.severity_consistency.exact_numerator, 1);
  assert.equal(m.severity_consistency.tolerance_numerator, 2);
  assert.equal(m.severity_consistency.denominator, 3);
  assert.deepEqual(m.severity_consistency.deviations.map((d) => d.case_id), ['C3']);
});

test('覆盖核对：有记录数/ok 数 + 截断计数', () => {
  const m = computeMetricSet([
    run({ input_coverage: { target_file_covered: true, coverage: { truncated: true } } }),
    run({ case_id: 'B', input_coverage: null }),
    run({ case_id: 'C', call_ok: false }),
  ]);
  assert.equal(m.coverage.numerator, 1, '仅第一个 ok run 有 coverage 记录');
  assert.equal(m.coverage.denominator, 2);
  assert.equal(m.coverage.truncated_runs, 1);
  assert.equal(m.coverage.target_file_covered_runs, 1);
});

test('mock 与 real 分列（永不合并统计）', () => {
  const m = computeMetrics([
    run({ llm_mode: 'mock', classifications: [cls('true_defect')] }),
    run({ llm_mode: 'real', classifications: [] }),
  ]);
  assert.deepEqual(Object.keys(m.by_mode).sort(), ['mock', 'real']);
  assert.equal(m.by_mode.mock.target_hit_rate.numerator, 1);
  assert.equal(m.by_mode.real.target_hit_rate.numerator, 0);
  assert.equal(m.by_mode.real.target_hit_rate.denominator, 1);
});
