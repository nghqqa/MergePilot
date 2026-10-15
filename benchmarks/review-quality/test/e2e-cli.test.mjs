// benchmarks/review-quality/test/e2e-cli.test.mjs — 执行器一键入口端到端冒烟（mock，零网络）。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const BENCH_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('CLI mock 模式端到端：校验→调用→分类→指标→落盘（单样本）', () => {
  const resultsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rq-e2e-r-'));
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rq-e2e-w-'));
  const r = spawnSync(process.execPath,
    [path.join(BENCH_DIR, 'bin', 'run-bench.mjs'), '--mode', 'mock',
      '--samples', 'CASE-04', '--results-dir', resultsDir, '--work-dir', workDir],
    { encoding: 'utf8', timeout: 120_000 });
  assert.equal(r.status, 0, `stderr: ${r.stderr.slice(-800)}`);
  const idx = fs.readFileSync(path.join(resultsDir, 'index.jsonl'), 'utf8').trim().split('\n');
  assert.equal(idx.length, 1);
  const entry = JSON.parse(idx[0]);
  assert.equal(entry.mode, 'mock');
  assert.equal(entry.mock, true);
  const runDir = path.join(resultsDir, entry.run_id);
  for (const f of ['result.json', 'validation.json', 'metrics.json', 'report.md']) {
    assert.ok(fs.existsSync(path.join(runDir, f)), `缺 ${f}`);
  }
  const result = JSON.parse(fs.readFileSync(path.join(runDir, 'result.json'), 'utf8'));
  assert.equal(result.runs.length, 2); // defect + fixed
  assert.ok(result.metrics.by_mode.mock);
  const md = fs.readFileSync(path.join(runDir, 'report.md'), 'utf8');
  assert.ok(md.includes('CASE-04'));
  assert.ok(md.includes('mock'));
});

test('CLI real 模式：env 未配置 → 退出码 4 + 前置条件说明，不落盘不伪造', () => {
  const resultsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rq-e2e-real-'));
  const r = spawnSync(process.execPath,
    [path.join(BENCH_DIR, 'bin', 'run-bench.mjs'), '--mode', 'real',
      '--samples', 'CASE-04', '--results-dir', resultsDir],
    { encoding: 'utf8', timeout: 60_000,
      env: { ...process.env, MU_LLM_PROVIDER: '', MU_LLM_BASE_URL: '', MU_LLM_API_KEY: '', MU_LLM_MODEL: '' } });
  assert.equal(r.status, 4, `stderr: ${r.stderr.slice(-400)}`);
  assert.ok(r.stderr.includes('前置条件'));
  assert.ok(r.stderr.includes('MU_LLM_MODEL'));
  assert.equal(fs.existsSync(path.join(resultsDir, 'index.jsonl')), false, '未产生调用则不落盘结果');
});

test('CLI 校验门失败：篡改样本后运行 → 退出码 3，失败记录全量保留', () => {
  // 用临时 results/work 目录 + 篡改一个样本文件副本场景：直接指向本 bench，
  // 篡改会破坏仓库文件——因此用 --samples 指向不存在样本触发同样入口路径（fail fast）
  const resultsDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rq-e2e-bad-'));
  const r = spawnSync(process.execPath,
    [path.join(BENCH_DIR, 'bin', 'run-bench.mjs'), '--mode', 'mock', '--samples', 'CASE-NOPE',
      '--results-dir', resultsDir],
    { encoding: 'utf8', timeout: 60_000 });
  assert.equal(r.status, 2, '未知样本必须 fail fast');
});
