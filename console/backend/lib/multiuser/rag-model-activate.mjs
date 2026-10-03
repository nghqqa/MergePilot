// console/backend/lib/multiuser/rag-model-activate.mjs — RAG 模型激活/回退编排（PR3/5）。
//
// 合同（任务书二.6/三.3）：
//  * sidecar 为部署件（compose 固定服务+模型卷挂载）——本模块不 spawn 容器；
//    激活=对部署提供的 sidecar 端点做三重门探测：/health → /manifest 字节级 pin →
//    /embed 冒烟（维度=manifest.dims）。任一不过=SIDECAR_START_FAILED，绝不 ACTIVE。
//  * sidecar manifest 由本模块按 v19 expected_files 生成（sidecar v2 格式）写入模型
//    目录 manifest.json（verify_manifest 的 drift 检查显式排除该文件——不自指）。
//  * 激活成功 = v19 setProviderActive('bge-m3') + ragtrial 注册表 activateSemanticModel
//    （dims/manifest 链式绑定+单活跃切换）；回退 = activateLocalModel + provider 切回
//    local-hash-v1。local-hash 永为安全基线。
//  * 消费侧零回归：注册表未切时 resolveModel 默认仍是既有活跃模型。
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

/** 生成 sidecar v2 manifest（由 v19 清单推导；返回 {bytes, sha256, manifest}）。 */
export function buildSidecarManifest(v19Manifest) {
  const manifest = {
    manifest_version: 2,
    model_id: v19Manifest.model_key,
    source: `official ${v19Manifest.source.official_channel} @ ${v19Manifest.source.files_revision.slice(0, 12)}（console 安装管线）`,
    pooling: v19Manifest.pooling ?? 'cls_l2',
    distance: v19Manifest.distance ?? 'cosine',
    dims: v19Manifest.dims,
    runtime: 'numpy-bert-v1',
    runtime_spec: { gelu: 'erf(scipy)', ln_eps: 1e-12, max_len: 128, dtype: 'F32', framework: 'numpy+tokenizers' },
    files: v19Manifest.files.map((f) => ({ name: f.path, sha256: f.sha256, bytes: f.bytes })),
  };
  const bytes = Buffer.from(JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  return { manifest, bytes, sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
}

/** 把 sidecar manifest 原子写入模型目录（部署把该目录挂给 sidecar 的 BGE_MODEL_DIR）。 */
export async function writeSidecarManifest(modelRoot, modelKey, v19Manifest) {
  const dir = path.join(modelRoot, modelKey);
  const { bytes, sha256, manifest } = buildSidecarManifest(v19Manifest);
  const tmp = path.join(dir, '.manifest.json.tmp');
  const final = path.join(dir, 'manifest.json');
  await fsp.mkdir(dir, { recursive: true });
  await fsp.writeFile(tmp, bytes);
  await fsp.rename(tmp, final);
  return { sha256, manifest, written: final };
}

/** 三重门探测（短超时；任何失败带错误码，不落内容）。 */
export async function probeSidecar(endpoint, { expectedManifestSha256, expectedDims, timeoutMs = 8000, fetchImpl = fetch }) {
  const base = String(endpoint).replace(/\/embed$/, '');
  const withTimeout = (ms) => { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; };
  const step = async (name) => {
    const t0 = Date.now();
    try { return { name, ms: Date.now() - t0 }; } finally { /* */ }
  };
  // ① /health
  let res;
  try { res = await fetchImpl(`${base}/health`, { signal: withTimeout(Math.min(timeoutMs, 3000)) }); }
  catch (e) { return { ok: false, stage: 'health', error_code: 'unreachable' }; }
  if (!res.ok) return { ok: false, stage: 'health', error_code: `health_http_${res.status}` };
  // ② /manifest 字节级 pin
  try { res = await fetchImpl(`${base}/manifest`, { signal: withTimeout(Math.min(timeoutMs, 3000)) }); }
  catch { return { ok: false, stage: 'manifest', error_code: 'unreachable' }; }
  if (!res.ok) return { ok: false, stage: 'manifest', error_code: `manifest_http_${res.status}` };
  const body = await res.text().catch(() => null);
  if (body === null) return { ok: false, stage: 'manifest', error_code: 'manifest_body' };
  const gotSha = crypto.createHash('sha256').update(Buffer.from(body, 'utf8')).digest('hex');
  if (gotSha !== expectedManifestSha256) {
    return { ok: false, stage: 'manifest', error_code: 'manifest_sha_mismatch',
      got_prefix: gotSha.slice(0, 12), expect_prefix: expectedManifestSha256.slice(0, 12) };
  }
  // ③ /embed 冒烟（维度=manifest.dims）
  try {
    res = await fetchImpl(`${base}/embed`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ input: ['activation-probe'] }),
      signal: withTimeout(timeoutMs) });
  } catch { return { ok: false, stage: 'embed_smoke', error_code: 'unreachable' }; }
  if (!res.ok) return { ok: false, stage: 'embed_smoke', error_code: `embed_http_${res.status}` };
  const emb = await res.json().catch(() => null);
  const vec = emb?.data?.[0]?.embedding;
  if (!Array.isArray(vec) || vec.length !== expectedDims) {
    return { ok: false, stage: 'embed_smoke', error_code: 'dims_mismatch',
      got: Array.isArray(vec) ? vec.length : null, expect: expectedDims };
  }
  return { ok: true, stages: ['health', 'manifest_pin', 'embed_smoke'] };
}

/** 激活（READY 门槛；三重门→v19 provider→ragtrial 注册表）。registryMod 为 ragtrial/store.mjs。 */
export async function activateModel({ pool, tenantId, modelKey, manifest, modelRoot, storeMod, registryMod, endpoint, onEvent }) {
  // ragtrial 注册表幂等就绪（首次激活前可能从未初始化过 RAG 试用面）
  const ragStore = await registryMod.createRagTrialStore({ pool });
  await ragStore.initSchema();
  const cur = await storeMod.getInstall(pool, { tenantId, modelKey });
  if (!cur) return { ok: false, http: 404, reason: 'model_not_installed' };
  if (cur.state === 'ACTIVE' && cur.active_provider === modelKey) {
    return { ok: true, state: 'ACTIVE', active_provider: modelKey, idempotent: true };
  }
  if (cur.state !== 'READY') return { ok: false, http: 409, reason: `illegal_state:${cur.state}` };
  if (!endpoint) return { ok: false, http: 409, reason: 'sidecar_endpoint_not_configured', hint: 'RAGTRIAL_EMBED_ENDPOINT' };

  const { sha256, manifest: sidecarManifest } = await writeSidecarManifest(modelRoot, modelKey, manifest);
  const probe = await probeSidecar(endpoint, { expectedManifestSha256: sha256, expectedDims: manifest.dims });
  if (!probe.ok) {
    await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'READY', to: 'SIDECAR_START_FAILED',
      errorCode: `${probe.stage}:${probe.error_code}` });
    onEvent?.('RAG_MODEL_SIDECAR_FAILED', { model_key: modelKey, stage: probe.stage, error_code: probe.error_code });
    return { ok: false, http: 422, reason: 'sidecar_probe_failed', detail: probe };
  }
  try {
    await registryMod.activateSemanticModel(pool, {
      modelId: manifest.model_key, dims: manifest.dims, manifest: sidecarManifest });
    const r = await storeMod.setProviderActive(pool, { tenantId, modelKey, provider: modelKey });
    onEvent?.('RAG_MODEL_ACTIVATED', { model_key: modelKey, dims: manifest.dims,
      manifest_sha_prefix: sha256.slice(0, 12), idempotent: !!r.idempotent });
    return { ok: true, state: 'ACTIVE', active_provider: modelKey, idempotent: !!r.idempotent };
  } catch (e) {
    await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'READY', to: 'ACTIVATION_FAILED',
      errorCode: String(e?.message ?? e).slice(0, 60) });
    onEvent?.('RAG_MODEL_ACTIVATION_FAILED', { model_key: modelKey, error_code: String(e?.message ?? 'registry').slice(0, 40) });
    return { ok: false, http: 500, reason: 'activation_failed' };
  }
}

/** 回退到 local-hash（provider+注册表双切；安装保留 READY 可重激活）。 */
export async function rollbackToLocal({ pool, tenantId, modelKey, storeMod, registryMod, onEvent }) {
  const ragStore = await registryMod.createRagTrialStore({ pool });
  await ragStore.initSchema();
  const cur = await storeMod.getInstall(pool, { tenantId, modelKey });
  if (!cur) return { ok: false, http: 404, reason: 'model_not_installed' };
  if (cur.active_provider !== modelKey) {
    return { ok: true, idempotent: true, state: cur.state, note: '已是 local-hash 基线' };
  }
  await registryMod.activateLocalModel(pool, {});
  const r = await storeMod.setProviderActive(pool, { tenantId, modelKey, provider: 'local-hash-v1' });
  onEvent?.('RAG_MODEL_ROLLED_BACK', { model_key: modelKey, to_provider: 'local-hash-v1', idempotent: !!r.idempotent });
  return { ok: true, state: 'READY', active_provider: 'local-hash-v1', idempotent: !!r.idempotent };
}
