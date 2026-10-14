// benchmarks/review-quality/lib/report.mjs — Markdown 证据报告生成器。
//
// 原则：报告如实呈现——mock 显式标 mock；失败与漏报逐条列出；分子/分母写明；
// 不为凑指标隐藏任何一条运行记录（含失败 run）。
const sevCell = (m) => `**${m.numerator}/${m.denominator}**`;

function modeHeader(mode) {
  if (mode === 'mock') return 'mock（脚本化发现，零网络——仅证明流水线，不代表模型能力）';
  if (mode === 'real') return 'real（生产基线入口 callLlmReviewer，llm_assist 路径）';
  if (mode === 'real-candidate') return 'real-candidate（候选提示词校准对照，同一 sanitize/schema 契约）';
  return mode;
}

function metricsSection(name, m) {
  const lines = [];
  lines.push(`### ${name}：${modeHeader(m.llm_mode)}`, '');
  lines.push('| 指标 | 分子/分母 | 明细 |');
  lines.push('|---|---|---|');
  lines.push(`| 调用完成率 | ${sevCell(m.call_completion)} | ${m.call_completion.failed.length === 0 ? '无失败' : `失败: ${m.call_completion.failed.map((f) => `${f.case_id}/${f.variant} ${f.code}`).join('; ')}`} |`);
  lines.push(`| 目标缺陷命中率（缺陷版） | ${sevCell(m.target_hit_rate)} | 命中: ${m.target_hit_rate.hit_cases.join(', ') || '—'}；漏报: ${m.target_hit_rate.misses.map((x) => `${x.case_id}(${x.failure_class})`).join(', ') || '—'}${m.target_hit_rate.excluded_call_failed.length ? `；调用失败不计入分母: ${m.target_hit_rate.excluded_call_failed.join(', ')}` : ''} |`);
  lines.push(`| 正确版误报数 | ${sevCell(m.clean_false_positives)}（误报条数/正确版run数） | ${m.clean_false_positives.per_case.filter((c) => c.false_positives > 0).map((c) => `${c.case_id}: ${c.false_positives}`).join('; ') || '全部为 0'} |`);
  lines.push(`| 发现准确性（precision） | ${sevCell(m.precision)} = true_defect/(true_defect+false_positive) | 五类计数: ${Object.entries(m.precision.class_counts).map(([k, v]) => `${k}=${v}`).join(', ')} |`);
  const sc = m.severity_consistency;
  lines.push(`| 严重度一致性 | 精确 ${sc.exact_numerator}/${sc.denominator}；容差±1内 ${sc.tolerance_numerator}/${sc.denominator} | ${sc.deviations.length === 0 ? '无超容差偏差' : `超容差: ${sc.deviations.map((d) => `${d.case_id} 期望${d.expected}/评${d.rated}`).join('; ')}`} |`);
  lines.push(`| 输入覆盖核对 | ${sevCell(m.coverage)}（有coverage记录/ok run） | 目标文件入载荷: ${m.coverage.target_file_covered_runs}/${m.coverage.denominator}；截断 run: ${m.coverage.truncated_runs} |`);
  lines.push('');
  return lines.join('\n');
}

/**
 * 渲染完整 Markdown 报告。
 * @param bench { run_meta, validation, runs, metrics } — 执行器产出的完整记录
 */
export function renderMarkdownReport(bench) {
  const { run_meta: meta, validation, runs, metrics } = bench;
  const L = [];
  L.push(`# review-quality 评测报告 — ${meta.run_id}`, '');
  L.push(`- 时间：${meta.timestamp}`);
  L.push(`- 模式：${modeHeader(meta.mode)}；模型：${meta.model ?? '—'}；提示词版本：${meta.prompt_version ?? '—'}`);
  L.push(`- 执行入口：${meta.entry}`);
  L.push(`- 样本清单版本：${meta.manifest_digest ?? '—'}；分类规则版本：${meta.rules_version ?? '—'}`);
  if (meta.mock) L.push(`- **mock 声明：${meta.mock}**`);
  L.push('');

  // 样本校验
  L.push('## 一、执行前样本校验', '');
  if (validation.manifest_drift.length > 0) {
    L.push(`**manifest 漂移 ${validation.manifest_drift.length} 项（基线不得漂移）：**`, '');
    for (const d of validation.manifest_drift) L.push(`- ${d.file}: ${d.reason ?? `${d.want} → ${d.got}`}`);
    L.push('');
  }
  L.push('| 样本 | 校验 | 失败类别 | 明细 |');
  L.push('|---|---|---|---|');
  for (const c of validation.cases) {
    L.push(`| ${c.case_id} | ${c.ok ? '通过' : `失败（${c.failure_class}）`} | ${c.failure_class ?? '—'} | ${c.detail.replace(/\|/g, '\\|')} |`);
  }
  L.push('');

  // 逐样本运行明细
  L.push('## 二、逐样本运行明细', '');
  L.push('| 样本 | 变体 | 调用 | 发现数 | 分类结果 | 输入覆盖 | 耗时ms |');
  L.push('|---|---|---|---|---|---|---|');
  for (const r of runs) {
    const cls = r.classifications.length === 0 ? '—'
      : r.classifications.map((c) => `#${c.finding_index + 1} ${c.category}${c.expected_severity ? `(期望${c.expected_severity}/评${c.rated_severity})` : ''}`).join('<br>');
    const cov = r.input_coverage
      ? `${r.input_coverage.target_file_covered ? '目标已入载荷' : '目标未入载荷'}(blocks ${r.input_coverage.coverage?.blocks_sent ?? '?'}/lines ${r.input_coverage.coverage?.lines_sent ?? '?'}${r.input_coverage.coverage?.truncated ? ',截断' : ''})`
      : '—';
    L.push(`| ${r.case_id} | ${r.variant} | ${r.call_ok ? 'ok' : `失败(${r.call_code})`} | ${r.findings_count} | ${cls.replace(/\|/g, '\\|')} | ${cov} | ${r.review?.latency_ms ?? '—'} |`);
  }
  L.push('');

  // 发现逐条评价
  L.push('## 三、发现逐条评价（五类）', '');
  let anyFinding = false;
  for (const r of runs) {
    if (r.classifications.length === 0) continue;
    anyFinding = true;
    L.push(`**${r.case_id} / ${r.variant}**（${r.llm_mode}）`, '');
    L.push('| # | 位置 | 严重度 | 摘要 | 分类 | 规则 | 说明 |');
    L.push('|---|---|---|---|---|---|---|');
    r.findings.forEach((f, i) => {
      const c = r.classifications[i] ?? {};
      L.push(`| ${i + 1} | ${f.path}:${f.line_start ?? '?'} | ${f.severity} | ${String(f.title).slice(0, 60).replace(/\|/g, '\\|')} | ${c.category ?? '—'} | ${c.rule_id ?? '—'} | ${(c.reason ?? '').replace(/\|/g, '\\|')}${c.expected_severity ? `；严重度期望${c.expected_severity}/评${c.rated_severity}` : ''} |`);
    });
    L.push('');
  }
  if (!anyFinding) { L.push('本轮全部运行零 finding。', ''); }

  // 指标
  L.push('## 四、指标（显式分子/分母，mock 与真实分列）', '');
  for (const [name, m] of Object.entries(metrics.by_mode)) {
    L.push(metricsSection(name, m));
  }

  // 边界与限制（如实）
  L.push('## 五、边界与限制', '');
  for (const lim of meta.limitations ?? []) L.push(`- ${lim}`);
  L.push('');
  L.push('---');
  L.push(`生成：benchmarks/review-quality（结果全量保留于 results/${meta.run_id}/；禁止反复运行直到命中）`);
  L.push('');
  return L.join('\n');
}
