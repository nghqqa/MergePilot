// ragtrial/embed.mjs — RAG trial embedding providers（LOCAL_RAG_TRIAL）。
//
// 零第三方依赖：
//  - local-hash-v1：确定性哈希嵌入（sha256 token 投影，256 维，L2 归一）。
//    同一文本永远同一向量（可重放）；model_digest = 规格说明书 canonical sha256，
//    规格或参数任何变化都会改变 digest → 索引绑定失效（index_stale）。
//  - remote（openai-compatible）：试验用外部 provider 占位；不可达时显式
//    provider_unavailable，绝不伪装为空结果。
//
// 边界：本模块只做向量化，不做任何检索/权限判断。

import crypto from 'node:crypto';

export const LOCAL_MODEL_ID = 'local-hash-v1';
export const EMBED_DIMS = 256;

export function localModelSpec() {
  return {
    model_id: LOCAL_MODEL_ID,
    kind: 'deterministic_hash',
    dims: EMBED_DIMS,
    tokenization: { latin: 'word_lower_min2', cjk: 'run_bigram_isolated_single' },
    weighting: '1_plus_log_tf',
    normalization: 'l2',
    hash: 'sha256_token_projection',
  };
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

export const sha256hex = (buf) => crypto.createHash('sha256').update(buf).digest('hex');
export function modelDigest(spec) { return sha256hex(canonicalJson(spec)); }

const CJK = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]/;
const isCjk = (ch) => CJK.test(ch);

// 分词：拉丁词（小写，≥2）+ CJK 连续段二元组（段长 1 时退化为单字）。
// 单字只对孤立 CJK 生效——避免 '测'（探测）误撞 '测试'/'检测'，保 precision。
export function tokenize(text) {
  const s = String(text ?? '').toLowerCase();
  const out = [];
  let latin = '';
  const flushLatin = () => {
    if (latin.length >= 2) out.push(latin);
    latin = '';
  };
  const chars = [...s];
  let cjkRun = '';
  const flushCjk = () => {
    if (!cjkRun) return;
    if (cjkRun.length === 1) out.push(cjkRun);
    else for (let i = 0; i + 1 < cjkRun.length; i++) out.push(cjkRun.slice(i, i + 2));
    cjkRun = '';
  };
  for (const ch of chars) {
    if (/[a-z0-9]/.test(ch)) { flushCjk(); latin += ch; continue; }
    flushLatin();
    if (isCjk(ch)) { cjkRun += ch; continue; }
    flushCjk();
  }
  flushLatin();
  flushCjk();
  return out;
}

function projectToken(token, dims) {
  const h = crypto.createHash('sha256').update(token, 'utf8').digest();
  const idx = ((h[0] << 16) | (h[1] << 8) | h[2]) % dims;
  const sign = (h[3] & 1) === 0 ? 1 : -1;
  return { idx, sign };
}

// 确定性嵌入：tf 加权 (1+ln tf) → sha256 投影 ±sign → L2 归一。
// 零向量（纯符号/空输入）用第 0 维 epsilon 占位，避免 pgvector 零向量距离 NaN。
export function embedLocal(text, dims = EMBED_DIMS) {
  const vec = new Float64Array(dims);
  const tf = new Map();
  for (const t of tokenize(text)) tf.set(t, (tf.get(t) ?? 0) + 1);
  for (const [tok, n] of tf) {
    const { idx, sign } = projectToken(tok, dims);
    vec[idx] += sign * (1 + Math.log(n));
  }
  let norm = 0;
  for (const v of vec) norm += v * v;
  norm = Math.sqrt(norm);
  if (norm === 0) vec[0] = 1e-6;
  else for (let i = 0; i < dims; i++) vec[i] /= norm;
  return Array.from(vec, (v) => +v.toFixed(8));
}

export function vectorLiteral(vec) {
  if (!Array.isArray(vec)) throw new Error('vectorLiteral: array required');
  if (vec.some((v) => !Number.isFinite(v))) throw new Error('vectorLiteral: NaN/Infinity forbidden');
  return `[${vec.join(',')}]`;
}

export class ProviderUnavailableError extends Error {
  constructor(detail) { super(`embedding provider unavailable: ${detail}`); this.name = 'ProviderUnavailableError'; }
}

// provider 解析：{kind:'local'} 或 {kind:'remote', endpoint, model, api_key_ref}。
// remote 仅试验占位：POST {input:[...]} → {data:[{embedding}...]}（openai 兼容形状）。
export function resolveProvider(env = process.env) {
  const endpoint = env.RAGTRIAL_EMBED_ENDPOINT;
  if (!endpoint) return { kind: 'local', model_id: LOCAL_MODEL_ID };
  return {
    kind: 'remote',
    model_id: env.RAGTRIAL_EMBED_MODEL_ID || 'remote-embed-trial',
    endpoint,
    api_key_ref: env.RAGTRIAL_EMBED_API_KEY_REF || null, // 仅引用名，绝不接收明文 key
    timeout_ms: Number(env.RAGTRIAL_EMBED_TIMEOUT_MS || 5000),
  };
}

export async function embedBatch(provider, texts, { fetchImpl = fetch } = {}) {
  if (provider.kind === 'local') {
    return texts.map((t) => embedLocal(t));
  }
  let res;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), provider.timeout_ms);
    res = await fetchImpl(provider.endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ input: texts, model: provider.model_id }),
      signal: ctrl.signal,
    });
    clearTimeout(timer);
  } catch (e) {
    throw new ProviderUnavailableError(String(e?.cause?.code || e?.message || e).slice(0, 120));
  }
  if (!res.ok) throw new ProviderUnavailableError(`HTTP ${res.status}`);
  let body;
  try { body = await res.json(); }
  catch { throw new ProviderUnavailableError('invalid json response'); }
  const data = body?.data;
  if (!Array.isArray(data) || data.length !== texts.length
      || data.some((d) => !Array.isArray(d?.embedding))) {
    throw new ProviderUnavailableError('malformed embedding response');
  }
  return data.map((d) => d.embedding.map((v) => +Number(v).toFixed(8)));
}
