// console/backend/lib/multiuser/rag-model-download.mjs — RAG 模型安装引擎（RAG-model-install PR2/5）。
//
// 合同（任务书二.5/二.7）：
//  * 断点续传（.part 追加；Range: bytes=N-）；完成后原子 rename；
//  * 严格官方源：URL 仅来自仓库版本化 manifest 模板（域名白名单），同域重定向放行、
//    跨域重定向拒绝；无任意 URL 下载；
//  * 磁盘预检：不足在开始前拒绝（INSUFFICIENT_DISK，不下半个文件）；
//  * 全文件 sha256 校验通过才 READY；任何不匹配=HASH_MISMATCH（拒绝激活，
//    state 机层面已不可能进 READY）；
//  * 失败 fail-closed：保留 local-hash provider；不静默降级；
//  * 幂等：重复 install 调用=继续/收敛（DB CAS + 内存锁单飞）；
//  * 日志与审计只落 元数据/状态/错误码/digest 前缀/耗时——绝不落
//    prompt/query/正文/模型内容/secret/token/cookie。
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';

/** 磁盘预检：可用空间 ≥ 需求 + 余量（默认 1GiB）。 */
export async function checkDiskSpace(dir, needBytes, { marginBytes = 1024 ** 3 } = {}) {
  await fsp.mkdir(dir, { recursive: true });
  const st = fs.statfs ? fs.statfsSync(dir) : null;
  if (!st) return { ok: true, note: 'statfs 不可用——跳过预检（容器内 ext4/overlay 常见）' };
  const avail = Number(st.bavail) * Number(st.bsize);
  return { ok: avail >= needBytes + marginBytes, avail, need: needBytes + marginBytes };
}

const active = new Map(); // `${tenantId}|${modelKey}` → AbortController

/** 单飞守卫：已有内存中进行中的安装则拒绝重复启动（409 语义由调用方落 HTTP）。 */
export function isInstalling(tenantId, modelKey) { return active.has(`${tenantId}|${modelKey}`); }

function officialUrl(manifest, file) {
  return manifest.source.download_url_template.replace('{path}', encodeURIComponent(file.path));
}

/** 校验重定向守卫：仅允许白名单内主机。 */
function sameHostGuard(allowedHosts) {
  return (urlStr) => {
    const u = new URL(urlStr);
    return allowedHosts.includes(u.hostname);
  };
}

/**
 * 安装/恢复安装（引擎主体）。调用方已过 RBAC/CSRF。
 * 返回 { ok, finalState, detail }——HTTP 语义由 API 层翻译。
 * onEvent(evt) 用于审计（只传元数据）。
 */
export async function runInstall({ pool, tenantId, modelKey, manifest, modelRoot, storeMod, onEvent, signal }) {
  const dir = path.join(modelRoot, modelKey);
  const key = `${tenantId}|${modelKey}`;
  if (active.has(key)) return { ok: false, reason: 'install_already_running' };
  const ctl = new AbortController();
  if (signal) signal.addEventListener('abort', () => ctl.abort(), { once: true });
  active.set(key, ctl);
  const t0 = Date.now();
  try {
    // ① 磁盘预检（开始前拒绝）
    const disk = await checkDiskSpace(dir, manifest.total_bytes);
    if (!disk.ok) {
      await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'UNINSTALLED', to: 'INSUFFICIENT_DISK', errorCode: 'insufficient_disk' })
        .catch(() => {});
      await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'DOWNLOAD_FAILED', to: 'INSUFFICIENT_DISK', errorCode: 'insufficient_disk' })
        .catch(() => {});
      onEvent?.('RAG_MODEL_INSTALL_REJECTED', { model_key: modelKey, error_code: 'insufficient_disk', need: disk.need, avail: disk.avail });
      return { ok: false, reason: 'insufficient_disk', detail: disk };
    }

    // ② 进 DOWNLOADING（CAS；UNINSTALLED/各失败态为合法前驱）
    const cur = await storeMod.getInstall(pool, { tenantId, modelKey });
    const from = cur?.state;
    const legal = ['UNINSTALLED', 'DOWNLOAD_FAILED', 'HASH_MISMATCH', 'INSUFFICIENT_DISK', 'DOWNLOADING', 'READY'];
    if (!legal.includes(from)) return { ok: false, reason: `illegal_state:${from}` };
    if (from !== 'DOWNLOADING') {
      const tr = await storeMod.transitionInstall(pool, { tenantId, modelKey, from, to: 'DOWNLOADING' });
      if (!tr.ok) return { ok: false, reason: 'lost_race', detail: { from } };
    }
    onEvent?.('RAG_MODEL_INSTALL_STARTED', { model_key: modelKey, manifest_version: manifest.manifest_version, files: manifest.files.length, total_bytes: manifest.total_bytes });

    // ③ 逐文件下载（断点续传 + 原子 rename + 大小上限）
    let doneBytes = 0;
    for (const f of manifest.files) {
      const finalP = path.join(dir, ...f.path.split('/'));
      const partP = finalP + '.part';
      await fsp.mkdir(path.dirname(finalP), { recursive: true });
      let have = 0;
      try { have = (await fsp.stat(partP)).size; } catch { have = 0; }
      if (have > f.bytes) { await fsp.rm(partP, { force: true }); have = 0; } // 超上限的残片丢弃
      if (have === f.bytes) {
        // 已完整（上次断点恰好齐）——走校验路径，跳过网络
        doneBytes += have; continue;
      }
      const url = officialUrl(manifest, f);
      const guard = sameHostGuard(manifest.source.allowed_download_hosts);
      let resp = await fetch(url, { signal: ctl.signal, headers: have ? { range: `bytes=${have}-` } : {} });
      let redirected = 0;
      while (resp.status >= 300 && resp.status < 400 && resp.headers.get('location') && redirected < 3) {
        const loc = new URL(resp.headers.get('location'), url);
        if (!guard(loc.toString())) { throw Object.assign(new Error('cross_host_redirect'), { code: 'cross_host_redirect' }); }
        resp = await fetch(loc, { signal: ctl.signal, headers: have ? { range: `bytes=${have}-` } : {} });
        redirected++;
      }
      const partial = resp.status === 206;
      if (!resp.ok && !(partial && have > 0)) throw Object.assign(new Error(`http_${resp.status}`), { code: `http_${resp.status}` });
      // 总大小守卫：正文长度不得超期望（防夹带）
      const lenHdr = Number(resp.headers.get('content-length') ?? 0);
      if (lenHdr && have + lenHdr > f.bytes) { throw Object.assign(new Error('size_exceeds_manifest'), { code: 'size_exceeds_manifest' }); }
      const out = fs.createWriteStream(partP, { flags: have ? 'a' : 'w' });
      const reader = resp.body.getReader();
      let got = have;
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          got += value.byteLength;
          if (got > f.bytes) { throw Object.assign(new Error('size_exceeds_manifest'), { code: 'size_exceeds_manifest' }); }
          if (!out.write(value)) await new Promise((r) => out.once('drain', r));
          if ((got & ~0xFFFFF) !== ((got - value.byteLength) & ~0xFFFFF)) {
            await storeMod.bumpProgress(pool, { tenantId, modelKey, downloadedBytes: doneBytes + got }).catch(() => {});
          }
        }
        // 等 flush 完成（Windows 下未关流即 rename 会 ENOENT/截断）
        await new Promise((resolve, reject) => { out.on('error', reject); out.end(resolve); });
      } catch (e) {
        out.destroy();
        throw e;
      }
      if (got !== f.bytes) throw Object.assign(new Error('truncated'), { code: 'truncated' });
      await fsp.rename(partP, finalP); // 原子
      doneBytes += f.bytes;
      await storeMod.bumpProgress(pool, { tenantId, modelKey, downloadedBytes: doneBytes }).catch(() => {});
    }
    onEvent?.('RAG_MODEL_DOWNLOAD_COMPLETED', { model_key: modelKey, bytes: doneBytes, seconds: Math.round((Date.now() - t0) / 1000) });

    // ④ 校验（全文件 sha256 过才 READY）
    await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'DOWNLOADING', to: 'VERIFYING' });
    const bad = [];
    for (const f of manifest.files) {
      const finalP = path.join(dir, ...f.path.split('/'));
      const h = crypto.createHash('sha256');
      await new Promise((resolve, reject) => {
        const rs = fs.createReadStream(finalP);
        rs.on('data', (c) => h.update(c)); rs.on('error', reject); rs.on('end', resolve);
      });
      const got = h.digest('hex');
      if (got !== f.sha256) bad.push({ path: f.path, got_prefix: got.slice(0, 16), expect_prefix: f.sha256.slice(0, 16) });
    }
    if (bad.length) {
      await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'VERIFYING', to: 'HASH_MISMATCH',
        errorCode: `sha256_mismatch:${bad.map((b) => b.path).join(',').slice(0, 120)}` });
      onEvent?.('RAG_MODEL_VERIFY_FAILED', { model_key: modelKey, mismatched: bad.length,
        files: bad.map((b) => b.path), got_prefixes: bad.map((b) => b.got_prefix) });
      return { ok: false, reason: 'hash_mismatch', detail: { bad } };
    }
    await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'VERIFYING', to: 'READY' });
    onEvent?.('RAG_MODEL_VERIFY_PASSED', { model_key: modelKey, files: manifest.files.length,
      sha256_prefixes: manifest.files.map((f) => f.sha256.slice(0, 12)) });
    return { ok: true, finalState: 'READY', detail: { bytes: manifest.total_bytes } };
  } catch (e) {
    const code = e?.code ?? (e?.name === 'AbortError' ? 'cancelled' : String(e?.message ?? e).slice(0, 60));
    await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'DOWNLOADING', to: 'DOWNLOAD_FAILED', errorCode: code }).catch(() => {});
    onEvent?.('RAG_MODEL_DOWNLOAD_FAILED', { model_key: modelKey, error_code: code, seconds: Math.round((Date.now() - t0) / 1000) });
    return { ok: false, reason: code === 'cancelled' ? 'cancelled' : 'download_failed', detail: { code } };
  } finally {
    active.delete(key);
  }
}

/** 独立重校验（READY/HASH_MISMATCH/DOWNLOAD_FAILED 均可发起；只读磁盘不下载）。 */
export async function runVerifyOnly({ pool, tenantId, modelKey, manifest, modelRoot, storeMod, onEvent }) {
  const dir = path.join(modelRoot, modelKey);
  const cur = await storeMod.getInstall(pool, { tenantId, modelKey });
  const from = cur?.state;
  if (!['READY', 'HASH_MISMATCH', 'DOWNLOAD_FAILED', 'VERIFYING'].includes(from)) {
    return { ok: false, reason: `illegal_state:${from}` };
  }
  if (from !== 'VERIFYING') {
    const tr = await storeMod.transitionInstall(pool, { tenantId, modelKey, from, to: 'VERIFYING' });
    if (!tr.ok) return { ok: false, reason: 'lost_race' };
  }
  const missing = []; const bad = [];
  for (const f of manifest.files) {
    const finalP = path.join(dir, ...f.path.split('/'));
    let h;
    try { h = crypto.createHash('sha256'); } catch { /* */ }
    try {
      await new Promise((resolve, reject) => {
        const rs = fs.createReadStream(finalP);
        rs.on('data', (c) => h.update(c)); rs.on('error', reject); rs.on('end', resolve);
      });
      if (h.digest('hex') !== f.sha256) bad.push(f.path);
    } catch { missing.push(f.path); }
  }
  if (missing.length || bad.length) {
    await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'VERIFYING', to: 'HASH_MISMATCH',
      errorCode: `sha256_mismatch:${[...missing, ...bad].join(',').slice(0, 120)}` });
    onEvent?.('RAG_MODEL_VERIFY_FAILED', { model_key: modelKey, mismatched: bad.length, missing: missing.length, files: [...missing, ...bad] });
    return { ok: false, reason: 'hash_mismatch', detail: { missing, bad } };
  }
  await storeMod.transitionInstall(pool, { tenantId, modelKey, from: 'VERIFYING', to: 'READY' });
  onEvent?.('RAG_MODEL_VERIFY_PASSED', { model_key: modelKey, files: manifest.files.length, reverify: true });
  return { ok: true, finalState: 'READY' };
}

/** 取消：中止内存任务 + 态回 UNINSTALLED（保留 .part 供续传）。 */
export async function cancelInstall({ pool, tenantId, modelKey, storeMod, onEvent }) {
  const ctl = active.get(`${tenantId}|${modelKey}`);
  ctl?.abort();
  const cur = await storeMod.getInstall(pool, { tenantId, modelKey });
  if (!cur) return { ok: false, reason: 'not_found' };
  const tr = await storeMod.transitionInstall(pool, { tenantId, modelKey, from: cur.state, to: 'UNINSTALLED' });
  onEvent?.('RAG_MODEL_INSTALL_CANCELLED', { model_key: modelKey, from_state: cur.state, ok: tr.ok });
  return { ok: true, detail: { transitioned: tr.ok, note: '.part 保留以供续传' } };
}

export function installLogSummary() {
  // 内存态摘要（进行中任务列表——仅 key 与字节数，零敏感）
  return [...active.keys()].map((k) => ({ key: k }));
}
