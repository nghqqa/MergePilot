// benchmarks/review-quality/test/run-review.test.mjs — 审查调用层单测（注入 fetchImpl，零真实网络）。
// 覆盖：生产入口 callLlmReviewer 真实路径（mock HTTP 层）、候选提示词路径、mock 模式、
// 输入覆盖核对、env 未配置时的稳定失败（绝不伪造结果）。
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import { runBaselineReview, runCandidateReview, runMockReview, computeInputCoverage, loadPromptDef } from '../lib/run-review.mjs';
import { LLM_PROMPT_VERSION } from '../../../console/backend/lib/multiuser/agents/llm.mjs';

const BENCH_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIFF = [
  'diff --git a/src/q.js b/src/q.js',
  '--- a/src/q.js',
  '+++ b/src/q.js',
  '@@ -1,4 +1,4 @@',
  ' function f(a){',
  "-  for (let i = a.length - 1; i > 0; i--) {",
  "+  for (let i = a.length - 1; i >= 0; i--) {",
  '     s += a[i];',
  ' }',
].join('\n');
const PR = { number: 1, title: '测试 PR', changed_files: 1, head: { sha: 'abc123' }, base: { ref: 'main' } };
const ENV = {
  MU_LLM_PROVIDER: 'openai_compatible', MU_LLM_BASE_URL: 'https://llm.example.test/v1',
  MU_LLM_API_KEY: 'test-key-not-placeholder', MU_LLM_MODEL: 'deepseek-chat',
};
const okResponse = (findings) => ({
  ok: true, status: 200, text: async () => JSON.stringify({
    choices: [{ message: { content: JSON.stringify({ findings, citations: [] }) } }],
  }),
});

test('computeInputCoverage：目标文件新增行确实进入模型载荷', () => {
  const cov = computeInputCoverage({ pr: PR, diff: DIFF, targetFile: 'src/q.js' });
  assert.equal(cov.target_file_covered, true);
  assert.equal(cov.target_added_lines_total, 1); // DIFF 只有一行 '+' 新增
  assert.ok(cov.target_added_lines_in_payload >= 1);
  assert.match(cov.input_digest, /^[0-9a-f]{64}$/);
});

test('computeInputCoverage：目标文件不在载荷 → target_file_covered=false（截断场景）', () => {
  // 构造一个只含其他文件 hunk 的场景：selectHunks 按 finding 选 hunk，findings 为空时
  // 回退全量 diff 截断——用超长无关 diff 挤掉目标文件不可行（保第一块），
  // 因此直接验证：目标路径不匹配时 covered=false。
  const cov = computeInputCoverage({ pr: PR, diff: DIFF, targetFile: 'src/other.js' });
  assert.equal(cov.target_file_covered, false);
});

test('runBaselineReview：真实路径（注入 fetch）走生产入口，system prompt=生产版', async () => {
  let captured = null;
  const fetchImpl = async (url, init) => {
    captured = { url, body: JSON.parse(init.body) };
    return okResponse([{ severity: 'P1', category: 'logic_bug',
      location: { path: 'src/q.js', line: 2 },
      summary: '循环条件 i > 0 跳过首元素',
      recommendation: '改为 i >= 0', confidence: 0.9 }]);
  };
  const r = await runBaselineReview({ pr: PR, diff: DIFF, env: ENV, fetchImpl });
  assert.equal(r.ok, true);
  assert.equal(r.llm_mode, 'real');
  assert.equal(r.entry, 'callLlmReviewer(production llm_assist path)');
  assert.equal(r.model, 'deepseek-chat');
  assert.equal(r.prompt_version, LLM_PROMPT_VERSION);
  assert.match(captured.url, /\/chat\/completions$/);
  assert.ok(captured.body.messages[0].content.startsWith('你是 PR 安全审查助手'), '必须用生产 SYSTEM_PROMPT');
  assert.ok(captured.body.messages[1].content.includes('untrusted_data'));
  assert.equal(r.findings[0].rule_id, 'LLM-logic_bug'); // 生产落库映射
  assert.equal(r.findings[0].title, '循环条件 i > 0 跳过首元素');
  assert.equal(r.tokens, null, 'token 计数生产入口不返回——如实记 null');
  assert.ok(r.coverage);
});

test('runBaselineReview：env 未配置 → 稳定 LLM_DISABLED，零网络零伪造', async () => {
  let called = false;
  const fetchImpl = async () => { called = true; throw new Error('must not be called'); };
  const r = await runBaselineReview({ pr: PR, diff: DIFF, env: {}, fetchImpl });
  assert.equal(r.ok, false);
  assert.match(r.code, /^LLM_DISABLED/);
  assert.equal(called, false);
  assert.deepEqual(r.findings, []);
});

test('runBaselineReview：HTTP 失败逐类显式（500/429）', async () => {
  const r500 = await runBaselineReview({ pr: PR, diff: DIFF, env: ENV,
    fetchImpl: async () => ({ ok: false, status: 500, text: async () => '' }) });
  assert.equal(r500.code, 'LLM_SERVER_ERROR');
  const r429 = await runBaselineReview({ pr: PR, diff: DIFF, env: ENV,
    fetchImpl: async () => ({ ok: false, status: 429, text: async () => '' }) });
  assert.equal(r429.code, 'LLM_RATE_LIMITED');
});

test('runCandidateReview：候选提示词替换 system 指令，schema 契约不变', async () => {
  let captured = null;
  const fetchImpl = async (url, init) => {
    captured = { body: JSON.parse(init.body) };
    return okResponse([]);
  };
  const promptDef = JSON.parse(fs.readFileSync(
    path.join(BENCH_DIR, 'prompts', 'candidates', 'severity-calibration-v2.json'), 'utf8'));
  const r = await runCandidateReview({ pr: PR, diff: DIFF, promptDef, env: ENV, fetchImpl });
  assert.equal(r.ok, true);
  assert.equal(r.llm_mode, 'real-candidate');
  assert.equal(r.prompt_version, 'bench-sev-cal-v2');
  assert.ok(captured.body.messages[0].content.includes('严重度锚定'), 'system 必须是候选提示词');
  assert.ok(captured.body.messages[1].content.includes('untrusted_data'), '出站裁剪契约不变');
});

test('runCandidateReview：输出违反 schema → LLM_SCHEMA_INVALID（不降级放行）', async () => {
  const r = await runCandidateReview({ pr: PR, diff: DIFF, env: ENV,
    promptDef: { prompt_version: 'x', system_prompt: 'p' },
    fetchImpl: async () => okResponse([{ severity: 'P9', category: 'logic_bug',
      location: { path: 'x', line: 1 }, summary: 's', recommendation: 'r' }]) });
  assert.equal(r.ok, false);
  assert.equal(r.code, 'LLM_SCHEMA_INVALID');
});

test('runMockReview：脚本化发现过 schema 校验，显式标 mock', async () => {
  const r = await runMockReview({ pr: PR, diff: DIFF, targetFile: 'src/q.js',
    scriptedFindings: [{ severity: 'P2', category: 'logic_bug',
      location: { path: 'src/q.js', line: 2 }, summary: '脚本化命中',
      recommendation: '修', confidence: 0.9 }] });
  assert.equal(r.ok, true);
  assert.equal(r.llm_mode, 'mock');
  assert.equal(r.model, 'bench-mock');
  assert.equal(r.latency_ms, 0);
  assert.equal(r.findings[0].title, '脚本化命中');
});

test('runMockReview：非法剧本发现 → MOCK_SCRIPT_INVALID（mock 也必须守 schema）', async () => {
  const r = await runMockReview({ pr: PR, diff: DIFF, targetFile: 'src/q.js',
    scriptedFindings: [{ severity: 'P9' }] });
  assert.equal(r.ok, false);
  assert.match(r.code, /^MOCK_SCRIPT_INVALID/);
});

test('loadPromptDef：baseline 读生产登记；未知候选报错', () => {
  const b = loadPromptDef(BENCH_DIR, 'baseline');
  assert.equal(b.prompt_version, LLM_PROMPT_VERSION);
  assert.ok(b.system_prompt.startsWith('你是 PR 安全审查助手'));
  assert.throws(() => loadPromptDef(BENCH_DIR, 'no-such-prompt'));
});
