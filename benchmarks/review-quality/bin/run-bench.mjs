#!/usr/bin/env node
// benchmarks/review-quality/bin/run-bench.mjs — 评测执行器一键入口。
//
// 流程：样本校验 → 输入覆盖核对 → 正式模型审查 → 发现分类 → 指标计算 → 导出证据报告。
//
// 用法：
//   node bin/run-bench.mjs --mode mock                       # 零网络自证（脚本化发现，结果标 mock）
//   node bin/run-bench.mjs --mode real                       # 生产基线入口（需 MU_LLM_* env）
//   node bin/run-bench.mjs --mode candidate --prompt severity-calibration-v2
//                                                            # 候选提示词对照（校准面，输出给主代理评估）
//   可选：--samples CASE-01,CASE-02  --results-dir <dir>  --work-dir <dir>
//
// 纪律：真实模式 env 未配置授权模型时立即退出（打印前置条件），绝不伪造结果；
// 每次运行产生新的 results/<run_id>/，绝不覆盖历史；禁止反复运行直到命中。
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { validateSamples } from '../lib/validate-samples.mjs';
import { runBaselineReview, runCandidateReview, runMockReview, computeInputCoverage, loadPromptDef } from '../lib/run-review.mjs';
import { loadRules, classifyFindings } from '../lib/classify.mjs';
import { computeMetrics } from '../lib/metrics.mjs';
import { renderMarkdownReport } from '../lib/report.mjs';
import { makeRunId, saveRunResults } from '../lib/results-store.mjs';

const BENCH_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function parseArgs(argv) {
  const args = { mode: 'mock', prompt: 'baseline', samples: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--mode') args.mode = argv[++i];
    else if (a === '--prompt') args.prompt = argv[++i];
    else if (a === '--samples') args.samples = argv[++i].split(',').map((s) => s.trim()).filter(Boolean);
    else if (a === '--results-dir') args.resultsDir = argv[++i];
    else if (a === '--work-dir') args.workDir = argv[++i];
    else if (a === '--help' || a === '-h') args.help = true;
  }
  return args;
}

function usage() {
  console.log(`用法: node bin/run-bench.mjs --mode mock|real|candidate [--prompt <候选id>] [--samples CASE-01,...]

模式:
  mock       零网络自证：脚本化发现（fixtures/mock-profile-trial.json），结果显式标 mock
  real       生产基线入口 callLlmReviewer（llm_assist 路径，与 trial 波同一入口）
  candidate  候选提示词对照（--prompt 指定 prompts/candidates/<id>），校准面输出给主代理评估

real/candidate 前置条件（env）:
  MU_LLM_PROVIDER=openai_compatible
  MU_LLM_BASE_URL=<授权模型的 OpenAI 兼容端点（https）>
  MU_LLM_API_KEY=<凭据>
  MU_LLM_MODEL=<授权模型名，trial 波口径为 deepseek-chat>

纪律: 结果全量保留于 results/（含失败）；绝不覆盖历史；禁止反复运行直到命中。`);
}

async function main() {
  const args = parseArgs(process.argv);
  if (args.help) { usage(); return 0; }
  if (!['mock', 'real', 'candidate'].includes(args.mode)) {
    console.error(`未知模式: ${args.mode}（可选 mock|real|candidate）`);
    return 2;
  }
  if (args.mode === 'candidate' && args.prompt === 'baseline') {
    console.error('candidate 模式必须 --prompt <候选id>（见 prompts/candidates/）');
    return 2;
  }
  if (args.mode !== 'mock' && args.prompt !== 'baseline') {
    console.error('提示词对照只在 candidate 模式（real 模式固定走生产基线提示词）');
    return 2;
  }

  const manifest = JSON.parse(fs.readFileSync(path.join(BENCH_DIR, 'manifest.json'), 'utf8'));
  const rules = loadRules(BENCH_DIR);
  const caseIds = args.samples ?? Object.keys(manifest.cases);
  for (const id of caseIds) {
    if (!manifest.cases[id]) { console.error(`manifest 中不存在样本: ${id}`); return 2; }
  }
  const manifestDigest = crypto.createHash('sha256')
    .update(JSON.stringify(manifest)).digest('hex').slice(0, 16);
  const workRoot = args.workDir
    ?? fs.mkdtempSync(path.join(os.tmpdir(), 'rq-bench-'));
  const resultsDir = args.resultsDir ?? path.join(BENCH_DIR, 'results');

  // ── 1. 执行前样本校验（校验不过：如实落盘失败记录后退出，不进入评测）──
  console.error(`[bench] 校验 ${caseIds.length} 组样本…`);
  const validation = await validateSamples({ benchDir: BENCH_DIR, manifest, caseIds, workRoot });
  if (!validation.all_ok) {
    console.error('[bench] 样本校验未通过——不进入评测。失败明细见落盘记录。');
    const failedRun = { run_id: makeRunId({ mode: `${args.mode}-validation-failed`, model: null, promptVersion: args.prompt }), timestamp: new Date().toISOString() };
    const dir = saveRunResults(resultsDir, failedRun.run_id, {
      run_meta: { ...failedRun, mode: args.mode, model: null, prompt_version: args.prompt,
        entry: 'validation-gate', manifest_digest: manifestDigest, rules_version: rules.schema_version,
        limitations: ['本次运行在样本校验门即失败，未产生任何模型调用。'] },
      validation, runs: [], metrics: { by_mode: {}, generated_at: new Date().toISOString() },
      report_md: renderMarkdownReport({ run_meta: { ...failedRun, mode: args.mode, model: null, prompt_version: args.prompt, entry: 'validation-gate', manifest_digest: manifestDigest, rules_version: rules.schema_version, limitations: ['本次运行在样本校验门即失败，未产生任何模型调用。'] }, validation, runs: [], metrics: { by_mode: {}, generated_at: new Date().toISOString() } }),
    });
    console.error(`[bench] 失败记录已保留: ${dir}`);
    return 3;
  }
  console.error('[bench] 样本校验全部通过（manifest 无漂移 / 规格完备 / 目标在 diff / 缺陷版测试失败 / 正确版测试通过）');

  // ── 2. 真实模式前置检查（缺 env 立即退出，打印前置条件）──
  const { resolveLlmProvider } = await import('../lib/run-review.mjs');
  let promptDef = null;
  if (args.mode !== 'mock') {
    const provider = resolveLlmProvider(process.env);
    if (provider.kind !== 'openai_compatible') {
      console.error(`[bench] 真实模型不可用（${provider.reason ?? 'disabled'}）——前置条件:

  MU_LLM_PROVIDER=openai_compatible
  MU_LLM_BASE_URL=<授权模型 OpenAI 兼容端点>
  MU_LLM_API_KEY=<凭据>
  MU_LLM_MODEL=<授权模型名，trial 波口径 deepseek-chat>

执行器已就绪，配置 env 后重跑本命令即可。本次未产生任何调用，不落盘结果。`);
      return 4;
    }
    promptDef = loadPromptDef(BENCH_DIR, args.prompt);
    console.error(`[bench] 真实调用: model=${provider.model} prompt=${args.mode === 'candidate' ? promptDef.prompt_version : 'llm-w31-v1(production)'}`);
  } else {
    console.error('[bench] mock 模式（脚本化发现，零网络，结果将显式标 mock）');
  }

  // ── 3-4. 逐样本×变体：覆盖核对 → 审查调用 → 分类 ──
  const mockProfile = args.mode === 'mock'
    ? JSON.parse(fs.readFileSync(path.join(BENCH_DIR, 'fixtures', 'mock-profile-trial.json'), 'utf8'))
    : null;
  const runs = [];
  for (const c of validation.cases) {
    const spec = c.spec;
    for (const variant of ['defect', 'fixed']) {
      const diff = c.diffs[variant];
      const pr = { number: 0, title: `${c.case_id} ${c.spec.name}（${variant}）`, changed_files: 2, head: { sha: c.base_commit.slice(0, 12) }, base: { ref: 'baseline' } };
      // 输入覆盖核对（纯函数、零网络——真实与 mock 一致执行）
      const inputCoverage = computeInputCoverage({ pr, diff, targetFile: spec.target_file });
      let review;
      if (args.mode === 'mock') {
        review = await runMockReview({ pr, diff, targetFile: spec.target_file,
          scriptedFindings: mockProfile.runs[`${c.case_id}:${variant}`] ?? [] });
      } else if (args.mode === 'real') {
        review = await runBaselineReview({ pr, diff, env: process.env });
      } else {
        review = await runCandidateReview({ pr, diff, promptDef, env: process.env });
      }
      const classifications = review.ok
        ? classifyFindings(review.findings, spec, rules, { variant })
        : [];
      const run = {
        case_id: c.case_id, variant,
        spec_ref: { name: spec.name, target_file: spec.target_file,
          expected_severity: spec.evaluation_rules.expected_severity.value,
          expected_severity_provenance: spec.evaluation_rules.expected_severity.provenance },
        llm_mode: review.llm_mode, entry: review.entry,
        call_ok: review.ok, call_code: review.code,
        diff_summary: { variant, bytes: Buffer.byteLength(diff),
          sha256: crypto.createHash('sha256').update(diff).digest('hex') },
        input_coverage: inputCoverage,
        findings_count: review.findings.length,
        findings: review.findings,
        classifications,
        review: { latency_ms: review.latency_ms, model: review.model,
          prompt_version: review.prompt_version, input_digest: review.input_digest,
          output_digest: review.output_digest, tokens: review.tokens,
          coverage: review.coverage },
      };
      runs.push(run);
      console.error(`[bench] ${c.case_id}/${variant}: ${review.ok ? `ok ${review.findings.length} findings` : `失败 ${review.code}`}`);
    }
  }

  // ── 5-6. 指标 → 报告 → 落盘（全量保留）──
  const metrics = computeMetrics(runs);
  const runId = makeRunId({ mode: args.mode, model: runs[0]?.review?.model ?? (args.mode === 'mock' ? 'bench-mock' : null), promptVersion: runs[0]?.review?.prompt_version ?? args.prompt });
  const limitations = [];
  if (args.mode === 'mock') limitations.push('mock 数据：脚本化模拟发现，非模型输出——指标仅证明流水线正确性，不代表模型审查能力。');
  if (args.mode === 'real' || args.mode === 'candidate') {
    limitations.push('本入口直连生产审查函数 callLlmReviewer（llm_assist 路径），未经完整栈（webhook/DB）；token 计数生产入口不返回，如实记 null。');
    limitations.push('分母仅含调用成功的 run；调用失败逐条列出（调用完成率），漏报按 covered_but_missed / target_not_covered 分档。');
  }
  const runMeta = { run_id: runId, timestamp: new Date().toISOString(), mode: args.mode,
    model: runs[0]?.review?.model ?? null, prompt_version: runs[0]?.review?.prompt_version ?? args.prompt,
    entry: runs[0]?.entry ?? '—', manifest_digest: manifestDigest, rules_version: rules.schema_version,
    mock: args.mode === 'mock' ? '脚本化发现（非模型输出）' : null, limitations };
  const payload = { run_meta: runMeta, validation, runs, metrics,
    report_md: renderMarkdownReport({ run_meta: runMeta, validation, runs, metrics }) };
  const dir = saveRunResults(resultsDir, runId, payload);
  console.error(`[bench] 完成。结果与报告: ${dir}`);
  return 0;
}

process.exit(await main());
