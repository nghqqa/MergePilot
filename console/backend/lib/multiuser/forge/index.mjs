// console/backend/lib/multiuser/forge/index.mjs — ForgeAdapter 契约层（Gitee 首版，G-1）。
//
// 职责：
//  * 规范事件 v1（ForgeEvent v1）字段契约与校验器——入队前、消费前各校验一次；
//    缺字段/坏字段→明确 reason（event_payload_invalid 族），可定位，不挂 queued（#389 教训）；
//  * 平台常量（forge_instance 默认行、Gitee 端点基址）；
//  * delivery_ref 确定性生成规则（ADR-003 §2.4 v1.1：不含接收时间戳——同一投递重试同 ref）。
//
// 契约来源：docs 计划 ADR-003（契约权威）+ r3work/forge-m0/GITEE-IMPLEMENTATION-DESIGN.md。
// 纪律：本层零网络、零 env 读取、零秘密；GitHub legacy 链（无 schema_version 的 7 字段
// payload）不经过本模块，行为原样。
import crypto from 'node:crypto';

export const FORGE_EVENT_SCHEMA_VERSION = 1;
// 首版启用的 forge_kind：仅 gitee（github v1 随 GitHub 迁入波启用；legacy GitHub 走原链）。
export const FORGE_KINDS_ENABLED = ['gitee'];

export const FORGE_INSTANCES = [
  { instance_id: 'gitee-cloud', forge_kind: 'gitee',
    api_base: 'https://gitee.com/api/v5', web_base: 'https://gitee.com',
    capability: { checks: 'not_provided', protection: 'not_provided', diff_mode: 'per_file' } },
  { instance_id: 'github-com', forge_kind: 'github',
    api_base: 'https://api.github.com', web_base: 'https://github.com',
    capability: { checks: 'native', protection: 'native', diff_mode: 'full' } },
];

/** 连接状态枚举（与 schema v24 CHECK 约束一致）。 */
export const FORGE_CONNECTION_STATES = ['pending', 'valid', 'denied', 'expired_revoked', 'unreachable', 'revoked'];

/**
 * 规范事件 v1 校验（纯函数）。入队前与消费前共用同一校验——契约单一来源。
 * 必填：schema_version=1 / event='pull_request' / forge_kind∈启用白名单 /
 *       delivery_ref（1-128 位安全字符）/ provider_repo_id（非空字符串，数字字符串化）/
 *       pr_number（正整数）/ head_sha（6-64 hex）。
 * Gitee 禁止伪装：installation_id 必须缺失或 null。
 * 返回 {ok:true} | {ok:false, reason, field?}——reason 进 job result，可定位。
 */
export function validateForgeEventV1(payload) {
  if (!payload || typeof payload !== 'object') return { ok: false, reason: 'event_payload_invalid', field: 'payload' };
  if (Number(payload.schema_version) !== FORGE_EVENT_SCHEMA_VERSION) {
    return { ok: false, reason: 'event_payload_invalid', field: 'schema_version' };
  }
  if (String(payload.event ?? '') !== 'pull_request') {
    return { ok: false, reason: 'event_payload_invalid', field: 'event' };
  }
  const kind = String(payload.forge_kind ?? '');
  if (!FORGE_KINDS_ENABLED.includes(kind)) {
    return { ok: false, reason: 'event_payload_invalid', field: 'forge_kind' };
  }
  const ref = String(payload.delivery_ref ?? '');
  if (!ref || ref.length > 128 || !/^[A-Za-z0-9_.:-]+$/.test(ref)) {
    return { ok: false, reason: 'event_payload_invalid', field: 'delivery_ref' };
  }
  const repoId = payload.provider_repo_id;
  if (repoId === undefined || repoId === null || !String(repoId).trim()
    || !/^[0-9]+$/.test(String(repoId))) {
    return { ok: false, reason: 'event_payload_invalid', field: 'provider_repo_id' };
  }
  const prNumber = Number(payload.pr_number ?? 0);
  if (!Number.isInteger(prNumber) || prNumber <= 0) {
    return { ok: false, reason: 'event_payload_invalid', field: 'pr_number' };
  }
  const headSha = String(payload.head_sha ?? '');
  if (!/^[0-9a-f]{6,64}$/i.test(headSha)) {
    return { ok: false, reason: 'event_payload_invalid', field: 'head_sha' };
  }
  if (payload.installation_id !== undefined && payload.installation_id !== null) {
    // 非 GitHub 平台不得携带 installation——防伪装合成（ADR-003 §3.2）
    return { ok: false, reason: 'event_payload_invalid', field: 'installation_id' };
  }
  return { ok: true };
}

/**
 * delivery_ref 确定性生成（无原生 delivery ID 的平台；ADR-003 §2.4 v1.1）。
 * 键=forge_kind + provider_repo_id + change_request_key + event_type + raw body sha256——
 * 不含接收时间戳/随机量：同一投递重试必须得到同 ref，否则传输去重失效。
 * 已登记残余风险：平台重投时 body 变化 → ref 变化 → 漏去重，由同 head 业务幂等兜底。
 */
export function deriveDeliveryRef({ forgeKind, providerRepoId, changeRequestKey, eventType, rawBody }) {
  const bodySha = crypto.createHash('sha256').update(String(rawBody ?? ''), 'utf8').digest('hex');
  return crypto.createHash('sha256')
    .update([forgeKind, providerRepoId, changeRequestKey, eventType, bodySha].join('|'), 'utf8')
    .digest('hex').slice(0, 32);
}
