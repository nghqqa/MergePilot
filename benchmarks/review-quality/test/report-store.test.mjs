// benchmarks/review-quality/test/report-store.test.mjs — 报告生成器与结果存储单测。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { renderMarkdownReport } from '../lib/report.mjs';
import { makeRunId, saveRunResults, listResults } from '../lib/results-store.mjs';
import { computeMetrics } from '../lib/metrics.mjs';

function miniBench({ mode = 'mock' } = {}) {
  const runs = [
    { case_id: 'CASE-T', variant: 'defect', llm_mode: mode, call_ok: true, call_code: null,
      findings_count: 1, findings: [{ rule_id: 'LLM-logic_bug', severity: 'P2', path: 'src/q.js',
        line_start: 3, title: '条件 i > 0 跳过首元素', remediation: '改 i >= 0' }],
      classifications: [{ finding_index: 0, category: 'true_defect', rule_id: 'CLA-TRUE-DEFECT',
        reason: '命中标记', expected_severity: 'P2', rated_severity: 'P2',
        severity_deviation: 0, severity_exact: true, severity_within_tolerance: true }],
      input_coverage: { target_file_covered: true, coverage: { blocks_sent: 1, lines_sent: 8, truncated: false } },
      review: { latency_ms: 5, model: mode === 'mock' ? 'bench-mock' : 'x', prompt_version: 'llm-w31-v1' } },
  ];
  const validation = { manifest_drift: [], cases: [{ case_id: 'CASE-T', ok: true, failure_class: null, detail: '全过' }], all_ok: true };
  const run_meta = { run_id: 'R1', timestamp: '2026-10-15T00:00:00Z', mode,
    model: mode === 'mock' ? 'bench-mock' : 'x', prompt_version: 'llm-w31-v1',
    entry: 'test-entry', manifest_digest: 'abcdef1234567890', rules_version: 'rq-rules-v2',
    mock: mode === 'mock' ? '脚本化发现（非模型输出）' : null,
    limitations: ['测试限制说明一', '测试限制说明二'] };
  return { run_meta, validation, runs, metrics: computeMetrics(runs) };
}

test('报告包含五节与诚实标注（mock 声明/分子分母/限制）', () => {
  const md = renderMarkdownReport(miniBench());
  for (const s of ['一、执行前样本校验', '二、逐样本运行明细', '三、发现逐条评价', '四、指标', '五、边界与限制']) {
    assert.ok(md.includes(s), `缺节: ${s}`);
  }
  assert.ok(md.includes('mock（脚本化发现，零网络'));
  assert.ok(md.includes('脚本化发现（非模型输出）'));
  assert.ok(md.includes('测试限制说明一'));
  assert.ok(md.includes('1/1')); // 命中率分子/分母
  assert.ok(md.includes('true_defect=1'));
  assert.ok(md.includes('期望P2/评P2')); // 严重度对照
});

test('校验失败场景：失败类别与漂移明细如实呈现', () => {
  const b = miniBench();
  b.validation = { manifest_drift: [{ file: 'samples/CASE-T/case.json', want: 'aa', got: 'bb' }],
    cases: [{ case_id: 'CASE-T', ok: false, failure_class: 'manifest_drift', detail: 'sha256 不符' }],
    all_ok: false };
  const md = renderMarkdownReport(b);
  assert.ok(md.includes('manifest 漂移 1 项'));
  assert.ok(md.includes('manifest_drift'));
  assert.ok(md.includes('sha256 不符'));
});

test('makeRunId：时间戳-模式-模型-提示词，特殊字符清洗', () => {
  const id = makeRunId({ timestamp: new Date('2026-10-15T09:30:00Z'), mode: 'mock', model: 'bench-mock', promptVersion: 'llm-w31-v1' });
  assert.equal(id, '20261015T0930-mock-bench-mock-llm-w31-v1');
  const id2 = makeRunId({ timestamp: new Date('2026-10-15T09:30:00Z'), mode: 'real candidate x', model: 'a/b', promptVersion: 'v 2' });
  assert.match(id2, /^20261015T0930-real-candidate-x-a-b-v-2$/);
});

test('saveRunResults：全量落盘 + 目录不覆盖 + index.jsonl 追加', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rq-store-test-'));
  const b = miniBench();
  const d1 = saveRunResults(dir, 'run-x', { ...b, report_md: renderMarkdownReport(b) });
  const d2 = saveRunResults(dir, 'run-x', { ...b, report_md: renderMarkdownReport(b) });
  assert.notEqual(d1, d2, '同 run_id 第二次落盘必须新目录（历史不覆盖）');
  assert.match(d2, /run-x-2$/);
  for (const f of ['result.json', 'validation.json', 'metrics.json', 'report.md']) {
    assert.ok(fs.existsSync(path.join(d1, f)), `缺文件 ${f}`);
  }
  const idx = listResults(dir);
  assert.equal(idx.length, 2);
  assert.equal(idx[0].run_id, 'run-x');
  assert.equal(idx[1].run_id, 'run-x-2');
  assert.equal(idx[0].mock, true);
});

test('listResults：空目录返回空数组（不抛错）', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rq-store-empty-'));
  assert.deepEqual(listResults(dir), []);
});
