// console/backend/lib/multiuser/rag-model-install.mjs — v20 RAG 模型安装控制面（v19 已被 C 波调用留痕占用，顺延）（store+状态机）。
//
// 设计合同（RAG-model-install 波任务书二节）：
//  * 状态机：UNINSTALLED→DOWNLOADING→VERIFYING→READY→ACTIVE；失败态
//    DOWNLOAD_FAILED/HASH_MISMATCH/INSUFFICIENT_DISK/SIDECAR_START_FAILED/
//    ACTIVATION_FAILED；回退=active_provider 切回 local-hash-v1（state 保持 ACTIVE
//    语义由 provider 字段承载——回退后 bge-m3 仍 READY 可重激活）。
//    实际上「回退到 local-hash」是 provider 切换而非安装状态回滚：state=ACTIVE 仅当
//    provider=bge-m3；回退后 state=READY + active_provider=local-hash-v1。
//  * fail-closed：任何失败态必须带 last_error_code；绝不允许失败态静默变 READY/ACTIVE。
//  * CAS：状态迁移仅允许合法前驱（下方 RMI_TRANSITIONS）；并发迁移单赢家
//    （UPDATE ... WHERE state=expected RETURNING）。
//  * manifest 版本化：仓库内 JSON（deploy/rag-model-install/*.manifest.json）为唯一
//    真源；落库行携带 manifest_version 供审计追溯；manifest 改动=发版可审查。
//  * 审计：只记 元数据/状态/错误码/耗时（由 API 层写入），绝不含
//    prompt/query/文档正文/模型内容/secret/token/cookie。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// 真源目录：仓库版本化 manifest（deploy/rag-model-install）。
// RMI_MANIFEST_DIR 仅供集成测试注入本地伪官方源（生产不设——api 层不透传该 env）。
const MANIFEST_DIR = process.env.RMI_MANIFEST_DIR
  || path.resolve(HERE, '../../../../deploy/rag-model-install');

// ── 状态机 ──
export const RMI_STATES = [
  'UNINSTALLED', 'DOWNLOADING', 'VERIFYING', 'READY', 'ACTIVE',
  'DOWNLOAD_FAILED', 'HASH_MISMATCH', 'INSUFFICIENT_DISK',
  'SIDECAR_START_FAILED', 'ACTIVATION_FAILED',
];
export const RMI_TRANSITIONS = {
  UNINSTALLED: ['DOWNLOADING', 'INSUFFICIENT_DISK', 'DOWNLOAD_FAILED'],
  DOWNLOADING: ['VERIFYING', 'DOWNLOAD_FAILED', 'INSUFFICIENT_DISK', 'UNINSTALLED'], // UNINSTALLED=用户取消（清点）
  VERIFYING: ['READY', 'HASH_MISMATCH', 'DOWNLOAD_FAILED', 'UNINSTALLED'],
  READY: ['ACTIVE', 'VERIFYING', 'DOWNLOADING', 'SIDECAR_START_FAILED', 'ACTIVATION_FAILED', 'UNINSTALLED'], // DOWNLOADING=重下（文件漂移/损坏）；激活探测/注册失败落对应失败态
  ACTIVE: ['READY', 'ACTIVATION_FAILED', 'VERIFYING'], // ACTIVE→READY=回退到 local-hash
  DOWNLOAD_FAILED: ['DOWNLOADING', 'UNINSTALLED'],
  HASH_MISMATCH: ['DOWNLOADING', 'UNINSTALLED'], // 哈希不匹配只能重下（拒绝激活）
  INSUFFICIENT_DISK: ['DOWNLOADING', 'UNINSTALLED'],
  SIDECAR_START_FAILED: ['READY', 'DOWNLOADING', 'UNINSTALLED'],
  ACTIVATION_FAILED: ['READY', 'ACTIVE', 'UNINSTALLED'],
};

/** 读取仓库内版本化 manifest（唯一真源；不猜测、不联网）。 */
export function loadModelManifest(modelKey) {
  const file = path.join(MANIFEST_DIR, `${modelKey}.modelscope.manifest.json`);
  const raw = fs.readFileSync(file, 'utf8');
  const m = JSON.parse(raw);
  if (m.model_key !== modelKey) throw new Error(`manifest model_key mismatch: ${m.model_key}`);
  const files = (m.files ?? []).map((f) => ({ path: String(f.path), sha256: String(f.sha256), bytes: Number(f.bytes) }));
  if (!files.length) throw new Error('manifest files empty');
  for (const f of files) {
    if (!/^[0-9a-f]{64}$/.test(f.sha256)) throw new Error(`bad sha256 for ${f.path}`);
  }
  // 下载 URL 严格域名：模板 host 必须在允许清单内（防 manifest 被改指向任意源）
  const url = new URL(m.source.download_url_template.replace('{path}', files[0].path));
  const host = url.hostname;
  if (!(m.source.allowed_download_hosts ?? []).includes(host)) {
    throw new Error(`download host not allowlisted: ${host}`);
  }
  return { ...m, files, total_bytes: Number(m.total_bytes) };
}

export function listInstallableModels() {
  return fs.readdirSync(MANIFEST_DIR)
    .filter((f) => f.endsWith('.modelscope.manifest.json'))
    .map((f) => f.replace('.modelscope.manifest.json', ''));
}

/** 幂等落库：按 (tenant, model_key) 建行（manifest 快照钉死）；已存在则原样返回。
 *  manifest_version 变化时【不】自动覆盖（版本升级须显式 re-pin 流程——本波不实现）。 */
export async function ensureInstallRow(pool, { tenantId, modelKey }) {
  const m = loadModelManifest(modelKey);
  const cur = await pool.query(
    `SELECT * FROM mu.rag_model_install WHERE tenant_id=$1 AND model_key=$2`, [tenantId, modelKey]);
  if (cur.rows.length) return { row: cur.rows[0], manifest: m, created: false };
  const r = await pool.query(
    `INSERT INTO mu.rag_model_install
       (tenant_id, model_key, manifest_version, source_url, revision, license,
        expected_files, total_bytes, state, active_provider)
     VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,'UNINSTALLED','local-hash-v1')
     ON CONFLICT (tenant_id, model_key) DO NOTHING RETURNING *`,
    [tenantId, modelKey, m.manifest_version, m.source.download_url_template,
      m.source.files_revision, m.license, JSON.stringify(m.files), m.total_bytes]);
  if (r.rows.length) return { row: r.rows[0], manifest: m, created: true };
  const again = await pool.query(
    `SELECT * FROM mu.rag_model_install WHERE tenant_id=$1 AND model_key=$2`, [tenantId, modelKey]);
  return { row: again.rows[0], manifest: m, created: false };
}

export async function getInstall(pool, { tenantId, modelKey }) {
  const r = await pool.query(
    `SELECT * FROM mu.rag_model_install WHERE tenant_id=$1 AND model_key=$2`, [tenantId, modelKey]);
  return r.rows[0] ?? null;
}

/** CAS 状态迁移：仅合法前驱放行，并发单赢家（返回 null=输家或非法迁移）。 */
export async function transitionInstall(pool, { tenantId, modelKey, from, to, errorCode = null, set = {} }) {
  const allowed = RMI_TRANSITIONS[from] ?? [];
  if (!allowed.includes(to)) return { ok: false, reason: 'illegal_transition', from, to };
  const sets = ['state=$3', 'updated_at=now()'];
  const params = [tenantId, modelKey, to];
  if (errorCode !== null || ['DOWNLOAD_FAILED', 'HASH_MISMATCH', 'INSUFFICIENT_DISK', 'SIDECAR_START_FAILED', 'ACTIVATION_FAILED'].includes(to)) {
    sets.push('last_error_code=$4'); params.push(errorCode);
  }
  for (const [k, v] of Object.entries(set)) {
    params.push(v); sets.push(`${k}=$${params.length}`);
  }
  params.push(from);
  const r = await pool.query(
    `UPDATE mu.rag_model_install SET ${sets.join(', ')}
      WHERE tenant_id=$1 AND model_key=$2 AND state=$${params.length} RETURNING *`, params);
  return r.rows.length ? { ok: true, row: r.rows[0] } : { ok: false, reason: 'lost_race_or_state_changed' };
}

/** 激活/回退：provider CAS（并发单赢家；state 与 provider 同步迁移）。
 *  provider 语义：'local-hash-v1'=回退（state→READY）；其他值=激活该模型（state→ACTIVE）。 */
export async function setProviderActive(pool, { tenantId, modelKey, provider, errorCode = null }) {
  const toState = provider === 'local-hash-v1' ? 'READY' : 'ACTIVE';
  const r = await pool.query(
    `UPDATE mu.rag_model_install SET
       state=$3, active_provider=$4, updated_at=now(),
       activated_at=CASE WHEN $4='local-hash-v1' THEN activated_at ELSE now() END,
       last_error_code=$5
     WHERE tenant_id=$1 AND model_key=$2
       AND (state, active_provider) IS DISTINCT FROM ($3, $4)
     RETURNING *`,
    [tenantId, modelKey, toState, provider, errorCode]);
  return r.rows.length ? { ok: true, row: r.rows[0], idempotent: false }
    : { ok: true, idempotent: true, row: await getInstall(pool, { tenantId, modelKey }) };
}

/** 进度更新（不迁移状态；下载器心跳）。 */
export async function bumpProgress(pool, { tenantId, modelKey, downloadedBytes }) {
  const r = await pool.query(
    `UPDATE mu.rag_model_install SET downloaded_bytes=$3, updated_at=now()
      WHERE tenant_id=$1 AND model_key=$2 AND state='DOWNLOADING' RETURNING downloaded_bytes`,
    [tenantId, modelKey, downloadedBytes]);
  return r.rows.length ? r.rows[0].downloaded_bytes : null;
}
