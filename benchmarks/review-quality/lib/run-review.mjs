// benchmarks/review-quality/lib/run-review.mjs — 评测执行器的审查调用层。
//
// 三种调用模式（结果 schema 中 llm_mode 字段显式区分，mock 与真实永不混同）：
//   * baseline  — 真实调用：直接走生产入口 callLlmReviewer（与 review-service.mjs
//                 llm_assist 阶段同一函数、同一 sanitize/schema 契约），提示词=
//                 生产 SYSTEM_PROMPT（prompt_version=LLM_PROMPT_VERSION）。
//                 需 env MU_LLM_PROVIDER/MU_LLM_BASE_URL/MU_LLM_API_KEY/MU_LLM_MODEL。
//   * candidate — 真实调用（提示词校准面）：同 sanitizeContextForLlm 出站裁剪 +
//                 同 validateLlmOutput 输出校验，仅 system 指令换为候选提示词。
//                 不触碰生产 SYSTEM_PROMPT——对比结果输出给主代理评估。
//   * mock      — 无网络：脚本化发现（先过 validateLlmOutput 保证 schema 一致），
//                 只用于流水线自证与单测，结果必须带 llm_mode='mock'。
//
// 失败与漏报显式分档（不静默）：call_failed / target_not_covered / covered_but_missed。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {
  callLlmReviewer, sanitizeContextForLlm, validateLlmOutput, resolveLlmProvider,
  LLM_PROMPT_VERSION,
} from '../../../console/backend/lib/multiuser/agents/llm.mjs';

export { LLM_PROMPT_VERSION, resolveLlmProvider };

/** 输入覆盖核对（纯函数、零网络）：目标文件的新增行是否真被送进模型载荷。
 *
 * 生产 sanitizeContextForLlm 的两种载荷形态都核对：
 *  a) precheck findings 锚定的 per-file hunks——hunk.path 即文件路径；
 *  b) 零发现回退（full_diff_truncated）——整个脱敏 diff 都在 hunk.lines 里，
 *     目标代码按行内容核对（rc.35.3 解耦波：静态零发现也要审得到代码）。
 */
export function computeInputCoverage({ pr, diff, targetFile }) {
  const { payload, inputDigest, coverage } = sanitizeContextForLlm({ pr, findings: [], diff });
  // 目标文件的新增行（内容，不含 '+' 前缀）
  const addedLines = [];
  let curFile = null;
  for (const raw of String(diff).split('\n')) {
    const m = raw.match(/^diff --git a\/(.+) b\/(.+)$/);
    if (m) { curFile = m[2]; continue; }
    if (curFile === targetFile || (curFile && curFile.endsWith(`/${targetFile}`))) {
      if (raw.startsWith('+') && !raw.startsWith('+++')) addedLines.push(raw.slice(1));
    }
  }
  const hunks = payload?.untrusted_data?.diff_hunks ?? [];
  const targetHunk = hunks.find((h) => h.path === targetFile || String(h.path).endsWith(`/${targetFile}`));
  const payloadText = hunks.flatMap((h) => h.lines ?? []).join('\n');
  const addedInPayload = addedLines.filter((l) => {
    const t = l.trim();
    return t !== '' && (payloadText.includes(t) || (targetHunk?.lines ?? []).some((hl) => String(hl).includes(t)));
  }).length;
  return {
    input_digest: inputDigest,
    coverage,
    target_file_covered: addedLines.length > 0 && addedInPayload > 0,
    target_added_lines_total: addedLines.length,
    target_added_lines_in_payload: addedInPayload,
  };
}

/** 生产 finding 形状映射（与 review-service.mjs llm_assist 落库映射逐字段一致）。 */
function mapToProductionFinding(f) {
  return {
    rule_id: `LLM-${f.category}`,
    severity: f.severity,
    confidence: Number(f.confidence ?? 0.5),
    path: String(f.location?.path ?? 'unknown').slice(0, 500),
    line_start: Number(f.location?.line ?? 0) || null,
    line_end: null,
    title: String(f.summary ?? '').slice(0, 300),
    evidence_ref: '',
    remediation: String(f.recommendation ?? '').slice(0, 1000),
    summary_masked: String(f.summary ?? '').slice(0, 120),
  };
}

function baseRecord({ mode, entry }) {
  return {
    ok: false, code: null, findings: [], input_digest: null, output_digest: null,
    model: null, prompt_version: null, latency_ms: null, coverage: null,
    tokens: null, // 生产入口不返回 token 计数（mu.agent_attempt.token_count 同口径）；可得时如实记录
    llm_mode: mode, entry,
  };
}

/** 真实调用——生产基线入口。env 未配置授权模型时返回稳定 code，绝不伪造结果。 */
export async function runBaselineReview({ pr, diff, env = process.env, fetchImpl = fetch }) {
  const rec = baseRecord({ mode: 'real', entry: 'callLlmReviewer(production llm_assist path)' });
  const provider = resolveLlmProvider(env);
  if (provider.kind !== 'openai_compatible') {
    rec.code = `LLM_DISABLED:${provider.reason ?? 'disabled'}`;
    return rec;
  }
  const r = await callLlmReviewer(provider, { pr, findings: [], diff, apiKey: env.MU_LLM_API_KEY },
    { fetchImpl });
  rec.ok = Boolean(r.ok);
  rec.code = r.ok ? null : String(r.code);
  rec.findings = r.ok ? (r.findings ?? []).map(mapToProductionFinding) : [];
  rec.input_digest = r.inputDigest ?? null;
  rec.output_digest = r.outputDigest ?? null;
  rec.model = r.model ?? provider.model;
  rec.prompt_version = r.promptVersion ?? LLM_PROMPT_VERSION;
  rec.latency_ms = r.latencyMs ?? null;
  rec.coverage = r.coverage ?? null;
  return rec;
}

/** 真实调用——候选提示词（校准面）。复用生产的出站裁剪与输出 schema。 */
export async function runCandidateReview({ pr, diff, promptDef, env = process.env, fetchImpl = fetch }) {
  const rec = baseRecord({ mode: 'real-candidate', entry: 'candidatePrompt(same sanitize/validate contract)' });
  const provider = resolveLlmProvider(env);
  if (provider.kind !== 'openai_compatible') {
    rec.code = `LLM_DISABLED:${provider.reason ?? 'disabled'}`;
    return rec;
  }
  const apiKey = env.MU_LLM_API_KEY;
  const { payload, inputDigest, coverage } = sanitizeContextForLlm({ pr, findings: [], diff });
  rec.input_digest = inputDigest;
  rec.coverage = coverage;
  const started = Date.now();
  let raw = null;
  try {
    const res = await fetchImpl(`${provider.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: `Bearer ${apiKey}` },
      body: JSON.stringify({ model: provider.model, temperature: 0,
        max_tokens: provider.maxTokens, response_format: { type: 'json_object' },
        messages: [{ role: 'system', content: String(promptDef.system_prompt ?? '') },
          { role: 'user', content: JSON.stringify(payload) }] }),
      signal: AbortSignal.timeout(provider.timeout),
    });
    if (res.status === 429) { rec.code = 'LLM_RATE_LIMITED'; return finish(rec, started); }
    if (res.status >= 500) { rec.code = 'LLM_SERVER_ERROR'; return finish(rec, started); }
    if (!res.ok) { rec.code = `LLM_HTTP_${res.status}`; return finish(rec, started); }
    raw = await res.text();
  } catch (e) {
    rec.code = String(e?.name ?? '').toLowerCase().includes('timeout') ? 'LLM_TIMEOUT' : 'LLM_UNAVAILABLE';
    return finish(rec, started);
  }
  if (!raw || Buffer.byteLength(raw) > 64 * 1024) { rec.code = 'LLM_OUTPUT_OVERSIZE'; return finish(rec, started); }
  let parsed = null;
  try {
    const outer = JSON.parse(raw);
    parsed = JSON.parse(outer?.choices?.[0]?.message?.content ?? raw);
  } catch { rec.code = 'LLM_INVALID_JSON'; return finish(rec, started); }
  const v = validateLlmOutput(parsed);
  if (!v.ok) { rec.code = v.code; return finish(rec, started); }
  rec.ok = true;
  rec.findings = v.findings.map(mapToProductionFinding);
  rec.output_digest = crypto.createHash('sha256').update(raw).digest('hex');
  rec.model = provider.model;
  rec.prompt_version = String(promptDef.prompt_version ?? promptDef.prompt_id ?? 'candidate');
  return finish(rec, started);
}

/** mock 调用——脚本化发现（过 validateLlmOutput 保证与真实输出同 schema）。仅测试/自证。 */
export async function runMockReview({ pr, diff, targetFile, scriptedFindings }) {
  const rec = baseRecord({ mode: 'mock', entry: 'scripted findings (schema-validated, no network)' });
  const v = validateLlmOutput({ findings: Array.isArray(scriptedFindings) ? scriptedFindings : [] });
  if (!v.ok) { rec.code = `MOCK_SCRIPT_INVALID:${v.code}`; return rec; }
  const cov = computeInputCoverage({ pr, diff, targetFile });
  rec.ok = true;
  rec.findings = v.findings.map(mapToProductionFinding);
  rec.input_digest = cov.input_digest;
  rec.coverage = cov.coverage;
  rec.model = 'bench-mock';
  rec.prompt_version = LLM_PROMPT_VERSION;
  rec.latency_ms = 0;
  rec.output_digest = crypto.createHash('sha256')
    .update(JSON.stringify(v.findings)).digest('hex');
  return rec;
}

function finish(rec, started) {
  rec.latency_ms = Date.now() - started;
  return rec;
}

/** 加载提示词定义（baseline 或候选）。 */
export function loadPromptDef(benchDir, promptId) {
  if (promptId === 'baseline' || !promptId) {
    return { role: 'baseline', prompt_version: LLM_PROMPT_VERSION,
      ...JSON.parse(fs.readFileSync(path.join(benchDir, 'prompts', 'baseline.json'), 'utf8')) };
  }
  const candsDir = path.join(benchDir, 'prompts', 'candidates');
  const file = fs.readdirSync(candsDir).find((f) => f === `${promptId}.json`)
    ?? fs.readdirSync(candsDir).find((f) => JSON.parse(fs.readFileSync(path.join(candsDir, f), 'utf8')).prompt_id === promptId);
  if (!file) throw new Error(`候选提示词不存在: ${promptId}`);
  return JSON.parse(fs.readFileSync(path.join(candsDir, file), 'utf8'));
}
