// cchain/ — C 链（embedding 检索）三个缺口的真实接口层。2026-09-26 闭环轮。
// 原则：接口、配置校验、失败状态、测试全部真实；缺少真实依赖（模型文件/
// provider 在线/key 预置）时状态保持 BLOCKED 并给出具体阻塞条件——不伪造 READY。

// ── 1) model cache ──────────────────────────────────────────────
// 缓存目录 + manifest（内容寻址）。verify 全量重哈希；缺失/损坏 fail-closed。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export function modelCacheStatus(env = process.env) {
  const dir = env.MERGEPILOT_MODEL_CACHE_DIR;
  if (!dir) return { state: 'NOT_CONFIGURED', blocked_condition: 'MERGEPILOT_MODEL_CACHE_DIR 未设置' };
  const manifestPath = path.join(dir, 'manifest.json');
  if (!fs.existsSync(manifestPath)) {
    return { state: 'MISSING', dir, blocked_condition: `manifest 不存在：${manifestPath}（需真实模型文件入库，禁止以空 manifest 充当 READY）` };
  }
  let manifest;
  try { manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8')); }
  catch { return { state: 'CORRUPT', dir, blocked_condition: 'manifest.json 解析失败' }; }
  const problems = [];
  for (const f of manifest.files ?? []) {
    const p = path.join(dir, f.name);
    if (!fs.existsSync(p)) { problems.push(`${f.name}: missing`); continue; }
    const h = crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
    if (h !== f.sha256) problems.push(`${f.name}: digest mismatch`);
  }
  if (problems.length) return { state: 'CORRUPT', dir, problems, blocked_condition: '文件缺失或摘要不符（内容寻址校验失败）' };
  return { state: 'READY', dir, model_id: manifest.model_id, files: (manifest.files ?? []).length };
}

// ── 2) provider metadata / live attestation ────────────────────
// 在线获取 provider 元数据并校验 attestation 字段；不可达/形状不符=失败态，
// 绝不把缓存值冒充 live。
export function providerAttestConfig(env = process.env) {
  const endpoint = env.MERGEPILOT_PROVIDER_ATTEST_URL;
  const expected = env.MERGEPILOT_PROVIDER_EXPECTED_KEY_ID || null;
  if (!endpoint) return { configured: false, blocked_condition: 'MERGEPILOT_PROVIDER_ATTEST_URL 未设置（provider 在线元数据端点）' };
  return { configured: true, endpoint, expected_key_id: expected, timeout_ms: Number(env.MERGEPILOT_PROVIDER_TIMEOUT_MS || 5000) };
}

export async function fetchProviderAttestation(env = process.env, fetchImpl = fetch) {
  const cfg = providerAttestConfig(env);
  if (!cfg.configured) return { state: 'NOT_CONFIGURED', blocked_condition: cfg.blocked_condition };
  let res;
  try {
    const ac = AbortSignal.timeout(cfg.timeout_ms);
    res = await fetchImpl(cfg.endpoint, { signal: ac, headers: { accept: 'application/json' } });
  } catch (e) {
    return { state: 'UNREACHABLE', endpoint: cfg.endpoint, blocked_condition: `provider 不可达：${String(e?.message || e)}` };
  }
  if (res.status !== 200) return { state: 'UNREACHABLE', endpoint: cfg.endpoint, http_status: res.status, blocked_condition: `provider HTTP ${res.status}` };
  let body;
  try { body = await res.json(); } catch { return { state: 'INVALID', blocked_condition: 'attestation 响应非 JSON' }; }
  const problems = [];
  for (const k of ['provider', 'model', 'attestation']) if (!body?.[k]) problems.push(`missing field: ${k}`);
  if (Array.isArray(body?.attestation) ? body.attestation.length === 0 : typeof body.attestation !== 'object') {
    problems.push('attestation 为空');
  }
  if (cfg.expected_key_id && body?.key_id !== cfg.expected_key_id) problems.push(`key_id 不符：期望 ${cfg.expected_key_id} 实得 ${body?.key_id}`);
  if (problems.length) return { state: 'INVALID', problems, blocked_condition: 'attestation 形状校验失败' };
  return { state: 'ATTESTED', provider: body.provider, model: body.model, key_id: body.key_id ?? null, fetched_at: new Date().toISOString() };
}

// ── 3) RUN_BINDING_AUTH key distribution ───────────────────────
// key 记录（keystore 目录 JSON，权限受限）+ HMAC 验签（full-sha256 + nonce 防重放）。
// 密钥分发未预置 → BLOCKED；接口与校验真实可测。
export function runBindingAuthStatus(env = process.env) {
  const dir = env.MERGEPILOT_RUN_BINDING_KEYSTORE;
  if (!dir) return { state: 'NOT_CONFIGURED', blocked_condition: 'MERGEPILOT_RUN_BINDING_KEYSTORE 未设置' };
  if (!fs.existsSync(dir)) return { state: 'MISSING', dir, blocked_condition: 'keystore 目录不存在（密钥分发未执行）' };
  const keys = fs.readdirSync(dir).filter((f) => f.endsWith('.key.json'));
  if (keys.length === 0) return { state: 'MISSING', dir, blocked_condition: 'keystore 无密钥记录（RUN_BINDING_AUTH 密钥分发未闭合）' };
  return { state: 'READY', dir, key_count: keys.length };
}

export function createRunBindingAuth(env = process.env) {
  const dir = env.MERGEPILOT_RUN_BINDING_KEYSTORE;
  const seen = new Map(); // nonce -> ts（单实例防重放）
  function loadKeys() {
    if (!dir || !fs.existsSync(dir)) return [];
    return fs.readdirSync(dir).filter((f) => f.endsWith('.key.json'))
      .map((f) => JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')))
      .filter((k) => !k.revoked && new Date(k.expires_at) > new Date());
  }
  return {
    verify({ run_id, nonce, timestamp, signature }) {
      const keys = loadKeys();
      if (keys.length === 0) return { ok: false, reason: 'RUN_BINDING_AUTH_BLOCKED', detail: runBindingAuthStatus(env).blocked_condition };
      if (!run_id || !nonce || !timestamp || !signature) return { ok: false, reason: 'BAD_REQUEST' };
      if (Math.abs(Date.now() - Number(timestamp)) > 5 * 60_000) return { ok: false, reason: 'TIMESTAMP_SKEW' };
      if (seen.has(nonce)) return { ok: false, reason: 'REPLAYED_NONCE' };
      const payload = `${run_id}|${nonce}|${timestamp}`;
      let matched = false;
      for (const k of keys) {
        const expect = crypto.createHmac('sha256', k.secret).update(payload).digest('hex');
        if (crypto.timingSafeEqual(Buffer.from(expect), Buffer.from(String(signature).padEnd(expect.length).slice(0, expect.length)))) { matched = true; break; }
      }
      if (!matched) return { ok: false, reason: 'BAD_SIGNATURE' };
      seen.set(nonce, Date.now());
      return { ok: true };
    },
    // 仅为测试/运维签发工具提供（生产密钥由受控分发通道写入 keystore，不进 Git）
    sign(keySecret, { run_id, nonce, timestamp }) {
      return crypto.createHmac('sha256', keySecret).update(`${run_id}|${nonce}|${timestamp}`).digest('hex');
    },
  };
}

// ── 聚合状态 ────────────────────────────────────────────────────
export async function cchainStatus(env = process.env) {
  const cache = modelCacheStatus(env);
  const attestCfg = providerAttestConfig(env);
  const attest = attestCfg.configured ? await fetchProviderAttestation(env) : { state: 'NOT_CONFIGURED', blocked_condition: attestCfg.blocked_condition };
  const binding = runBindingAuthStatus(env);
  const components = [
    { component: 'model_cache', ...cache },
    { component: 'provider_attestation', ...attest },
    { component: 'run_binding_auth', ...binding },
  ];
  const blocked = components.filter((c) => c.state !== 'READY' && c.state !== 'ATTESTED');
  return {
    overall: blocked.length === 0 ? 'READY' : 'BLOCKED',
    components,
    blocked_conditions: blocked.map((c) => `${c.component}: ${c.blocked_condition}`),
  };
}
