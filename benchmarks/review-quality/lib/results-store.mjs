// benchmarks/review-quality/lib/results-store.mjs — 结果组织与留痕。
//
// 组织：results/<run_id>/ 一次运行一个目录（run_id=时间戳-模式-模型-提示词版本），
// 内含 result.json（全量）、validation.json、metrics.json、report.md；
// 另 results/index.jsonl 追加一行索引。目录已存在则加后缀，**绝不覆盖历史**
// ——保留所有历史结果（含失败），禁止反复运行直到命中。
import fs from 'node:fs';
import path from 'node:path';

export function makeRunId({ timestamp = new Date(), mode, model, promptVersion }) {
  const t = timestamp.toISOString().replaceAll(/[:.\-]/g, '').slice(0, 13);
  const safe = (s) => String(s ?? 'na').replace(/[^A-Za-z0-9._-]+/g, '-').slice(0, 40);
  return `${t}-${safe(mode)}-${safe(model)}-${safe(promptVersion)}`;
}

export function saveRunResults(resultsDir, runId, payload) {
  let dir = path.join(resultsDir, runId);
  let n = 2;
  while (fs.existsSync(dir)) { dir = path.join(resultsDir, `${runId}-${n}`); n += 1; }
  fs.mkdirSync(dir, { recursive: true });
  const write = (name, content) => fs.writeFileSync(path.join(dir, name), content);
  write('result.json', `${JSON.stringify(payload, null, 2)}\n`);
  write('validation.json', `${JSON.stringify(payload.validation, null, 2)}\n`);
  write('metrics.json', `${JSON.stringify(payload.metrics, null, 2)}\n`);
  if (payload.report_md) write('report.md', payload.report_md);
  fs.mkdirSync(resultsDir, { recursive: true });
  fs.appendFileSync(path.join(resultsDir, 'index.jsonl'),
    `${JSON.stringify({ run_id: path.basename(dir), dir: path.basename(dir),
      mode: payload.run_meta?.mode, model: payload.run_meta?.model,
      prompt_version: payload.run_meta?.prompt_version,
      timestamp: payload.run_meta?.timestamp, all_ok: payload.validation?.all_ok,
      mock: payload.run_meta?.mode === 'mock' })}\n`);
  return dir;
}

/** 读取历史结果索引（供 README/报告引用历史轮次）。 */
export function listResults(resultsDir) {
  const idx = path.join(resultsDir, 'index.jsonl');
  if (!fs.existsSync(idx)) return [];
  return fs.readFileSync(idx, 'utf8').split('\n').filter((l) => l.trim())
    .map((l) => { try { return JSON.parse(l); } catch { return { unparsable: l.slice(0, 80) }; } });
}
