// cchain/ — C 链（embedding 检索）三个缺口的真实接口层。2026-09-26 闭环轮。
// 原则：接口、配置校验、失败状态、测试全部真实；缺少真实依赖（模型文件/
// provider 在线/key 预置）时状态保持 BLOCKED 并给出具体阻塞条件——不伪造 READY。

// ── 1) model cache ──────────────────────────────────────────────
// 缓存目录 + manifest（内容寻址）。verify 全量重哈希；缺失/损坏 fail-closed。
// D-2（rc.12）：生产 bge-m3 pytorch_model.bin=2,271,145,830B（≈2.12GiB）超出 Node
// Buffer 2GiB 硬上限——原 readFileSync 整读抛 "File size is greater than 2 GiB" →
// /api/cchain/status HTTP 500 → model_cache 对生产真实模型结构性不可能 READY。
// 现改为 fs.createReadStream 分块 pipe 进 sha256（流式，内存 O(1)）：摘要逐字节
// 等价于整读（sha256 只看字节序，与分块无关），manifest 校验语义逐项保持。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

// 流式 sha256：createReadStream 分块 update，任何读错误经 stream error 事件 reject
// （EACCES/EPERM=权限拒绝；不把文件内容带进错误消息——只上抛 OS 错误码）。
async function sha256FileStream(p) {
  const hash = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(p)) hash.update(chunk);
  return hash.digest('hex');
}

export async function modelCacheStatus(env = process.env) {
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
    let h;
    try {
      h = await sha256FileStream(p);
    } catch (e) {
      // 权限拒绝不 crash：fail-closed 记 permission denied → CORRUPT（D-2 要求）。
      // 其他读错误同样 fail-closed（绝不让 status 端点 500）。只报文件名+错误码。
      if (e?.code === 'EACCES' || e?.code === 'EPERM') { problems.push(`${f.name}: permission denied`); continue; }
      problems.push(`${f.name}: read error (${e?.code ?? 'unknown'})`);
      continue;
    }
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
// rc.16（不可读密钥误报 READY 缺陷）：状态与验签统一以「实际可加载密钥」为准——
// 逐文件 try/catch（读失败/解析失败均跳过，绝不抛出）；仅存在但不可用者计入
// unusable 计数并以稳定状态码呈现（不含路径/权限/内容细节）。
function loadKeysDetailed(dir) {
  const detail = { keys: [], total: 0, unreadable: 0, unparseable: 0, unusable: 0 };
  if (!dir || !fs.existsSync(dir)) return detail;
  const files = fs.readdirSync(dir).filter((f) => f.endsWith('.key.json'));
  detail.total = files.length;
  for (const f of files) {
    let k;
    try {
      k = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
    } catch {
      detail.unparseable += 1; // 读失败（EACCES 等）与解析失败同归 unusable，区分仅计数
      continue;
    }
    try {
      if (k.revoked || new Date(k.expires_at) <= new Date()) { detail.unusable += 1; continue; }
      detail.keys.push(k);
    } catch {
      detail.unparseable += 1;
    }
  }
  return detail;
}
function loadKeys(dir) {
  return loadKeysDetailed(dir).keys;
}
export function runBindingAuthStatus(env = process.env) {
  const dir = env.MERGEPILOT_RUN_BINDING_KEYSTORE;
  if (!dir) return { state: 'NOT_CONFIGURED', blocked_condition: 'MERGEPILOT_RUN_BINDING_KEYSTORE 未设置' };
  if (!fs.existsSync(dir)) return { state: 'MISSING', not_distributed: true, dir, blocked_condition: 'keystore 目录不存在（密钥分发未执行=NOT_DISTRIBUTED）' };
  const d = loadKeysDetailed(dir);
  // 有效密钥=可读+可解析+未撤销+未过期。仅存在但不可用者不构成 READY——
  // 状态面与验签面同源（loadKeysDetailed），杜绝「不可读密钥误报 READY」。
  if (d.keys.length === 0) {
    const reason = d.total === 0
      ? 'keystore 无密钥记录（RUN_BINDING_AUTH 密钥分发未闭合=NOT_DISTRIBUTED）'
      : `keystore 密钥均不可用（RUN_BINDING_KEYS_UNUSABLE：total=${d.total} unusable=${d.unusable + d.unparseable}）`;
    return {
      state: d.total === 0 ? 'MISSING' : 'BLOCKED',
      not_distributed: d.total === 0 || undefined,
      keys_unusable: d.total > 0 || undefined,
      dir, key_count: 0,
      blocked_condition: reason,
    };
  }
  return { state: 'READY', dir, key_count: d.keys.length };
}

export function createRunBindingAuth(env = process.env, { now = Date.now } = {}) {
  const dir = env.MERGEPILOT_RUN_BINDING_KEYSTORE;
  // M-1（2026-09-27 加固波）：nonce 存储有界化——TTL + 容量上限 + FIFO 淘汰。
  // * 安全不变量：TTL ≥ 2× 时间窗（钳制下限 10min）。原因：被清理 nonce 的
  //   "捕获重放"（同 nonce+原 timestamp）必须先撞上 ±5min TIMESTAMP_SKEW；
  //   若允许 TTL < skew 窗口，重放载荷在清理后仍新鲜 → 绕过防重放。env 只能调高。
  // * 容量默认 8192（对齐 rag-live 有界 FIFO 惯例）为内存兜底：仅"验签成功"
  //   占用槽位（无密钥者无法灌入）；达上限先清过期，仍满则 FIFO 淘汰最旧
  //   （此时被淘汰 nonce 若在 TTL 内被同 key 重签重放理论上可过——需持有有效
  //   密钥且窗口受限，属已声明的有界权衡）。
  // * MERGEPILOT_NONCE_CAP 可覆盖（供容量边界测试）；now 时钟注入仅供测试。
  const NONCE_TTL_MS = Math.max(Number(env.MERGEPILOT_NONCE_TTL_MS ?? 0) || 10 * 60_000, 10 * 60_000);
  const NONCE_CAP = Number(env.MERGEPILOT_NONCE_CAP || 8192);
  const seen = new Map(); // nonce -> 过期时刻（Map 插入序 = FIFO 依据）
  function pruneExpired(at = now()) {
    for (const [n, exp] of seen) if (exp <= at) seen.delete(n);
  }
  function rememberNonce(nonce) {
    const at = now();
    pruneExpired(at);
    if (seen.size >= NONCE_CAP) seen.delete(seen.keys().next().value); // FIFO 淘汰最旧
    seen.set(nonce, at + NONCE_TTL_MS);
  }
  function loadKeys() {
    // rc.16（不可读密钥缺陷修复）：逐文件 try/catch——读失败（EACCES 等）或
    // 解析失败均跳过而非抛出；verify 面由此获得干净的 RUN_BINDING_AUTH_BLOCKED
    // 4xx 拒绝（原实现抛异常 → 路由 500），与状态面同源不误报。
    if (!dir || !fs.existsSync(dir)) return [];
    const keys = [];
    for (const f of fs.readdirSync(dir).filter((f) => f.endsWith('.key.json'))) {
      try {
        const k = JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8'));
        if (!k.revoked && new Date(k.expires_at) > new Date()) keys.push(k);
      } catch { /* 读失败/解析失败：跳过该文件（fail-closed 计入无可用密钥） */ }
    }
    return keys;
  }
  return {
    verify({ run_id, nonce, timestamp, signature }) {
      const keys = loadKeys();
      if (keys.length === 0) return { ok: false, reason: 'RUN_BINDING_AUTH_BLOCKED', detail: runBindingAuthStatus(env).blocked_condition };
      if (!run_id || !nonce || !timestamp || !signature) return { ok: false, reason: 'BAD_REQUEST' };
      // L-1（2026-09-27 加固波）：timestamp 必须是有限数值。NaN/Infinity/非数字
      // 此前会绕过 skew 比较（NaN>x 恒 false）——现 fail-closed 拒绝。
      const ts = Number(timestamp);
      if (!Number.isFinite(ts)) return { ok: false, reason: 'INVALID_TIMESTAMP' };
      if (Math.abs(now() - ts) > 5 * 60_000) return { ok: false, reason: 'TIMESTAMP_SKEW' };
      if (seen.has(nonce)) return { ok: false, reason: 'REPLAYED_NONCE' };
      const payload = `${run_id}|${nonce}|${timestamp}`;
      let matched = false;
      for (const k of keys) {
        const expect = crypto.createHmac('sha256', k.secret).update(payload).digest('hex');
        if (crypto.timingSafeEqual(Buffer.from(expect), Buffer.from(String(signature).padEnd(expect.length).slice(0, expect.length)))) { matched = true; break; }
      }
      if (!matched) return { ok: false, reason: 'BAD_SIGNATURE' };
      rememberNonce(nonce);
      return { ok: true };
    },
    // 运维/测试观测：nonce 存储当前占用（清理过期后的实时值）
    nonceStats() { pruneExpired(); return { size: seen.size, capacity: NONCE_CAP, ttl_ms: NONCE_TTL_MS }; },
    // 仅为测试/运维签发工具提供（生产密钥由受控分发通道写入 keystore，不进 Git）
    sign(keySecret, { run_id, nonce, timestamp }) {
      return crypto.createHmac('sha256', keySecret).update(`${run_id}|${nonce}|${timestamp}`).digest('hex');
    },
  };
}

// ── 聚合状态 ────────────────────────────────────────────────────
export async function cchainStatus(env = process.env) {
  const cache = await modelCacheStatus(env); // D-2：modelCacheStatus 已改 async（流式 sha256）
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
