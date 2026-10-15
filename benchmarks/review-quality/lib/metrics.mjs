// benchmarks/review-quality/lib/metrics.mjs — 指标计算（全部显式分子/分母）。
//
// 纪律（契约 §5）：mock 与真实调用分列；漏报与输入问题分档（covered_but_missed /
// target_not_covered / call_failed 互不掩盖）；保留全部结果，绝不只报命中项。
//
// 输入 runs 形状（执行器产出，每样本×变体一条）：
//   { case_id, variant: 'defect'|'fixed', llm_mode, call_ok, call_code,
//     input_coverage: { target_file_covered, ... }, classifications: [...] }
import { CATEGORIES } from './classify.mjs';

const emptyCounts = () => Object.fromEntries(CATEGORIES.map((c) => [c, 0]));

/**
 * 计算单组（同一 llm_mode）指标。分组由调用方完成（computeMetrics 按模式分组）。
 */
export function computeMetricSet(runs) {
  const defectRuns = runs.filter((r) => r.variant === 'defect');
  const fixedRuns = runs.filter((r) => r.variant === 'fixed');
  const okRuns = runs.filter((r) => r.call_ok);

  // 1) 调用完成率：ok / total
  const call_completion = {
    numerator: okRuns.length,
    denominator: runs.length,
    failed: runs.filter((r) => !r.call_ok)
      .map((r) => ({ case_id: r.case_id, variant: r.variant, code: r.call_code })),
  };

  // 2) 目标缺陷命中率：缺陷版 run 中有 ≥1 true_defect 的 / 可评定（调用成功）的缺陷版 run
  const evaluableDefect = defectRuns.filter((r) => r.call_ok);
  const hitRuns = evaluableDefect.filter((r) => r.classifications.some((c) => c.category === 'true_defect'));
  const misses = evaluableDefect.filter((r) => !hitRuns.includes(r)).map((r) => ({
    case_id: r.case_id,
    // 失败分档：目标代码未进模型载荷=输入问题（target_not_covered）；
    // 覆盖充分但无命中=模型漏报（covered_but_missed）。互不掩盖。
    failure_class: r.input_coverage?.target_file_covered ? 'covered_but_missed' : 'target_not_covered',
  }));
  const target_hit_rate = {
    numerator: hitRuns.length,
    denominator: evaluableDefect.length,
    hit_cases: hitRuns.map((r) => r.case_id),
    misses,
    excluded_call_failed: defectRuns.filter((r) => !r.call_ok).map((r) => r.case_id),
  };

  // 3) 正确版误报：fixed run 上分类为 false_positive 的 finding 数（分母=正确版 run 数）
  const clean_false_positives = {
    numerator: fixedRuns.filter((r) => r.call_ok)
      .reduce((n, r) => n + r.classifications.filter((c) => c.category === 'false_positive').length, 0),
    denominator: fixedRuns.filter((r) => r.call_ok).length,
    per_case: fixedRuns.filter((r) => r.call_ok).map((r) => ({
      case_id: r.case_id,
      false_positives: r.classifications.filter((c) => c.category === 'false_positive').length,
    })),
  };

  // 4) 发现准确性：true_defect / (true_defect + false_positive)；其余三类单列计数
  const counts = emptyCounts();
  for (const r of runs) for (const c of r.classifications) counts[c.category] += 1;
  const precision = {
    numerator: counts.true_defect,
    denominator: counts.true_defect + counts.false_positive,
    class_counts: counts, // duplicate_report / ambiguous_spec / general_suggestion 不进 precision 分母，但全部显式呈现
  };

  // 5) 严重度一致性（仅 defect 变体的 true_defect 参与比较）
  const comparable = [];
  for (const r of evaluableDefect) {
    for (const c of r.classifications) {
      if (c.category === 'true_defect' && c.expected_severity) {
        comparable.push({ case_id: r.case_id, expected: c.expected_severity,
          rated: c.rated_severity, deviation: c.severity_deviation,
          exact: c.severity_exact, within_tolerance: c.severity_within_tolerance });
      }
    }
  }
  const severity_consistency = {
    exact_numerator: comparable.filter((c) => c.exact).length,
    tolerance_numerator: comparable.filter((c) => c.within_tolerance).length,
    denominator: comparable.length,
    deviations: comparable.filter((c) => !c.within_tolerance),
    detail: comparable,
  };

  // 6) 覆盖情况：有 coverage 记录的 ok run / ok run；截断计数
  const cov = {
    numerator: okRuns.filter((r) => r.input_coverage).length,
    denominator: okRuns.length,
    truncated_runs: okRuns.filter((r) => r.input_coverage?.coverage?.truncated).length,
    target_file_covered_runs: okRuns.filter((r) => r.input_coverage?.target_file_covered).length,
  };

  return {
    llm_mode: runs[0]?.llm_mode ?? 'unknown',
    call_completion, target_hit_rate, clean_false_positives, precision,
    severity_consistency, coverage: cov,
  };
}

/** 按模式分组计算；真实基线与真实候选也分列（candidate 是校准面对照，不入主指标口径）。 */
export function computeMetrics(runs) {
  const groups = new Map();
  for (const r of runs) {
    const key = r.llm_mode ?? 'unknown';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  const byMode = {};
  for (const [mode, rs] of groups) byMode[mode] = computeMetricSet(rs);
  return { by_mode: byMode, generated_at: new Date().toISOString() };
}
