// console/backend/lib/multiuser/agents/matrix-transport.mjs — Wave 3.4：AgentTeams 任务传输层（Matrix 正式通道）。
//
// 方案依据（2026-09-29 裁决）：AgentTeams worker 的任务消费通道=Matrix room 消息
// （上游 delegate_task 即向 worker room 发消息）；controller DAG 自动消费链在
// 223ddc2 断（Team Leader 心跳 cron 硬编码/TaskMeta 依赖 delegate），故 MergePilot
// 以受测 adapter 直接承担委派语义——与上游 Leader 委派同协议、同通道。
//
// 安全契约：
//  * 仅与配置的 Matrix homeserver 通信（MU_AGENTTEAMS_MATRIX_* 部署级 env，凭据仅
//    进程环境——不落库/日志/审计；登录 token 模块内缓存，401 自动重登一次）；
//  * 派发=结构化信封（含 @mention + mp_task JSON：taskId/correlationId/submissionId/
//    role/brief 脱敏摘要）；txnId 由 submissionId 派生 → 重发幂等（同 event）；
//  * 收集四重绑定：固定 room（controller 登记的 worker room）+ 精确 sender（worker
//    matrixUserID）+ 时间窗（派发之后，防重放/过期）+ submissionId marker（回复须
//    含本轮 marker）——其余一律忽略；
//  * 超时/不可达 → 稳定 reason（MT_*），调用方 fail-closed（死信/拒绝），绝不伪造结果；
//  * 本模块不接触 GitHub/DB/LLM 凭据；brief 由 sanitizeBrief 白名单产生。

export const MT_LIMITS = Object.freeze({
  replyPollMs: 5_000, defaultRoleTimeoutMs: 240_000, markerGuardMs: 2_000,
  maxBodyChars: 4_000,
});

/** 部署级 Matrix 配置解析（fail-closed：缺任一 → rejected）。 */
export function resolveMatrixConfig(env = process.env) {
  const url = String(env.MU_AGENTTEAMS_MATRIX_URL ?? '').trim();
  const user = String(env.MU_AGENTTEAMS_MATRIX_USER ?? '').trim();
  const password = String(env.MU_AGENTTEAMS_MATRIX_PASSWORD ?? '').trim();
  if (!url || !user || !password) return { kind: 'rejected', reason: 'MT_NOT_CONFIGURED' };
  if (!/^https?:\/\//.test(url)) return { kind: 'rejected', reason: 'MT_BAD_URL' };
  return { kind: 'matrix', baseUrl: url.replace(/\/+$/, ''), user, password };
}

let cachedLogin = null; // { baseUrl, user, token } —— 模块级；换配置即失效
export function __resetMatrixLoginForTests() { cachedLogin = null; }

/** 登录（缓存+失效重登）。返回 {ok, token} 或 {ok:false, reason}。 */
export async function matrixLogin(cfg, fetchImpl = fetch) {
  if (cachedLogin && cachedLogin.baseUrl === cfg.baseUrl && cachedLogin.user === cfg.user) {
    return { ok: true, token: cachedLogin.token };
  }
  try {
    const r = await fetchImpl(`${cfg.baseUrl}/_matrix/client/v3/login`, { method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ type: 'm.login.password', user: cfg.user, password: cfg.password }) });
    if (!r.ok) return { ok: false, reason: `MT_LOGIN_HTTP_${r.status}` };
    const j = await r.json().catch(() => null);
    if (!j?.access_token) return { ok: false, reason: 'MT_LOGIN_NO_TOKEN' };
    cachedLogin = { baseUrl: cfg.baseUrl, user: cfg.user, token: j.access_token };
    return { ok: true, token: j.access_token };
  } catch { return { ok: false, reason: 'MT_LOGIN_UNREACHABLE' }; }
}

/** 任务信封（@mention 前缀 + mp_task JSON——worker 原生消费格式）。 */
export function buildTaskEnvelope({ workerMatrixId, taskId, correlationId, submissionId, role, brief }) {
  const marker = `[mp:${submissionId}]`;
  const envelope = JSON.stringify({ mp_task: { taskId, correlationId, submissionId, role,
    brief: String(brief ?? '').slice(0, MT_LIMITS.maxBodyChars) } });
  const body = `${workerMatrixId} task ${taskId} ${marker}\n${envelope}\n`
    + `Reply with exactly: ${marker} then ONE compact JSON on the same message obeying your role contract. No other text.`;
  return { body, marker };
}

/**
 * 派发一个任务（幂等：txnId=sha-like 稳定串由 submissionId 派生——同 submissionId
 * 重发得到同一 event id，Matrix PUT txn 语义天然幂等）。返回 {ok, eventId, ts}。
 */
export async function sendTaskDelegation(cfg, { room, workerMatrixId, taskId, correlationId,
  submissionId, role, brief, fetchImpl = fetch, tsNow = Date.now }) {
  const lg = await matrixLogin(cfg, fetchImpl);
  if (!lg.ok) return lg;
  const { body } = buildTaskEnvelope({ workerMatrixId, taskId, correlationId, submissionId, role, brief });
  const txnId = 'mp' + Buffer.from(String(submissionId)).toString('base64url').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 60)
    + '-' + String(taskId).replace(/[^A-Za-z0-9_-]/g, '');
  try {
    const r = await fetchImpl(`${cfg.baseUrl}/_matrix/client/v3/rooms/${encodeURIComponent(room)}/send/m.room.message/${txnId}?access_token=${lg.token}`,
      { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ msgtype: 'm.text', body }) });
    if (r.status === 401) { // token 过期 → 重登一次
      cachedLogin = null;
      const lg2 = await matrixLogin(cfg, fetchImpl);
      if (!lg2.ok) return lg2;
      const r2 = await fetchImpl(`${cfg.baseUrl}/_matrix/client/v3/rooms/${encodeURIComponent(room)}/send/m.room.message/${txnId}?access_token=${lg2.token}`,
        { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ msgtype: 'm.text', body }) });
      if (!r2.ok) return { ok: false, reason: `MT_SEND_HTTP_${r2.status}` };
      const j2 = await r2.json().catch(() => null);
      return { ok: true, eventId: j2?.event_id ?? null, ts: tsNow() };
    }
    if (!r.ok) return { ok: false, reason: `MT_SEND_HTTP_${r.status}` };
    const j = await r.json().catch(() => null);
    return { ok: true, eventId: j?.event_id ?? null, ts: tsNow() };
  } catch { return { ok: false, reason: 'MT_SEND_UNREACHABLE' }; }
}

/** 从回复正文提取最后一个平衡 JSON 对象（固定末 `}`、从右向左找可解析完整对象）。 */
export function extractReplyJson(text) {
  const s = String(text ?? '');
  const end = s.lastIndexOf('}');
  if (end === -1) return null;
  for (let start = s.lastIndexOf('{', end); start >= 0; start = s.lastIndexOf('{', start - 1)) {
    try {
      const v = JSON.parse(s.slice(start, end + 1));
      if (v && typeof v === 'object' && !Array.isArray(v)) return v;
    } catch { /* keep scanning left */ }
  }
  return null;
}

/**
 * 收集一个任务的 worker 回复（四重绑定：room 固定/sender 精确/ts 时间窗/marker）。
 * 轮询至超时；忽略一切不满足绑定的消息。返回 {ok, json, raw, ts} 或 {ok:false, reason}。
 */
export async function collectReply(cfg, { room, expectedSender, marker, sinceTs,
  timeoutMs = MT_LIMITS.defaultRoleTimeoutMs, fetchImpl = fetch, sleepImpl = (ms) => new Promise((r) => setTimeout(r, ms)),
  tsNow = Date.now }) {
  const deadline = tsNow() + timeoutMs;
  for (;;) {
    const lg = await matrixLogin(cfg, fetchImpl);
    if (!lg.ok) return lg;
    try {
      const r = await fetchImpl(`${cfg.baseUrl}/_matrix/client/v3/rooms/${encodeURIComponent(room)}/messages?dir=b&limit=20&access_token=${lg.token}`);
      if (!r.ok) return { ok: false, reason: `MT_READ_HTTP_${r.status}` };
      const j = await r.json().catch(() => null);
      const chunk = j?.chunk ?? [];
      // 时间序（旧→新），取第一条满足全部绑定的回复
      const candidates = chunk.filter((e) => e?.type === 'm.room.message'
        && String(e.sender ?? '') === String(expectedSender)
        && Number(e.origin_server_ts ?? 0) > Number(sinceTs)
        && String(e.content?.body ?? '').includes(marker)).reverse();
      for (const e of candidates) {
        const body = String(e.content?.body ?? '');
        const json = extractReplyJson(body);
        if (json) return { ok: true, json, raw: body.slice(0, MT_LIMITS.maxBodyChars), ts: e.origin_server_ts };
        // marker 在但无 JSON → 继续（worker 可能分多条；窗口内等下一条）
      }
    } catch { return { ok: false, reason: 'MT_READ_UNREACHABLE' }; }
    if (tsNow() > deadline) return { ok: false, reason: 'MT_REPLY_TIMEOUT' };
    await sleepImpl(MT_LIMITS.replyPollMs);
  }
}
