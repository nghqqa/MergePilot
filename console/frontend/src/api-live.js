// api-live.js — 正式契约 v2（console/API-AUTH-MERGE-V0 @ 7ccecb9）适配层 + console_v3 只读联调适配。
//
// 边界：
// - 本文件只定义请求形状、响应分类与数据映射，不包含任何后端业务逻辑；
// - 契约端点（session/capabilities/pulls/merge）后端尚未实现——这里只定义形状与分类，
//   不从 UI 伪装"已接通"；console_v3 /api/runs 是当前唯一真实只读 HTTP 查询（数据自带
//   shadow/fixture 标签，永不标为真实运行）；
// - 服务端 Cookie（mp_session）是会话权威；浏览器不保存 token/私钥/长期凭证。

export const SESSION_PATH = '/api/auth/session';
export const CAPABILITIES_PATH = '/api/me/capabilities';
export const PULLS_PATH = '/api/pulls';

// 契约 §0.1：全部副作用方法（含 PATCH）需要 X-CSRF-Token；GET 不需要。
const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export function sessionUrl() {
  return SESSION_PATH;
}

// 契约 §0.5：repo 寻址走查询参数 ?repo=owner%2FName，不进路径段。
export function capabilitiesUrl(repo) {
  return `${CAPABILITIES_PATH}?repo=${encodeURIComponent(repo)}`;
}

export function pullsUrl(repo, { state, limit, offset } = {}) {
  const q = new URLSearchParams({ repo: repo });
  if (state) q.set('state', state);
  if (limit != null) q.set('limit', String(limit));
  if (offset != null) q.set('offset', String(offset));
  return `${PULLS_PATH}?${q.toString()}`;
}

export function pullUrl(repo, prNumber) {
  return `${PULLS_PATH}/${prNumber}?repo=${encodeURIComponent(repo)}`;
}

// ---- 审批（只读列表/详情：契约归属 C-4/C-11，后端未实现；决策接口本轮不接） ----
// 红线：fixture 演示（ApprovalsPage 内存变换）与本适配层彻底分离；
// 真实批准/拒绝仅在认证+授权+票据+决策端点全部就绪后启用，届时必须携带 CSRF 与幂等键。
export const APPROVALS_PATH = '/api/approvals';

export function approvalsUrl({ status } = {}) {
  const q = new URLSearchParams();
  if (status) q.set('status', status);
  return q.toString() ? `${APPROVALS_PATH}?${q.toString()}` : APPROVALS_PATH;
}

export function approvalUrl(ticketId) {
  return `${APPROVALS_PATH}/${encodeURIComponent(ticketId)}`;
}

export function approvalDecisionUrl(ticketId) {
  return `${APPROVALS_PATH}/${encodeURIComponent(ticketId)}/decision`;
}

export function requiresCsrf(method) {
  return WRITE_METHODS.has(String(method).toUpperCase());
}

// 写操作请求头（本轮无任何写调用；为契约接线预留，缺 token 时本地拦截而不是静默裸发）。
export function buildWriteHeaders(method, csrfToken) {
  const headers = {};
  if (requiresCsrf(method)) {
    if (!csrfToken) {
      throw new Error('CSRF token 缺失 — 写操作被本地拦截（契约 §0.1：副作用方法必须携带 X-CSRF-Token）');
    }
    headers['X-CSRF-Token'] = csrfToken;
  }
  return headers;
}

export function readCsrfCookie(doc = (typeof document !== 'undefined' ? document : undefined), name = 'mp_csrf') {
  if (!doc) return null;
  const row = doc.cookie.split(';').map((s) => s.trim()).find((s) => s.startsWith(`${name}=`));
  return row ? decodeURIComponent(row.slice(name.length + 1)) : null;
}

// GET /api/auth/session 响应分类（契约 §0/§1：未登录 401 JSON、服务端不重定向）。
// 返回统一形状：{ state, user?, expiresAt?, reason? }
//   authed | anonymous(not_authenticated) | expired(session_expired) |
//   forbidden(not_a_member) | auth_unavailable(503) | not_implemented(404) | unavailable(network/其它)
export function classifySession({ status, body } = {}) {
  const reason = body?.error?.reason ?? null;
  if (status === 200) {
    if (body?.user) return { state: 'authed', user: body.user, expiresAt: body.expires_at ?? null };
    return { state: 'anonymous', reason: 'not_authenticated' };
  }
  if (status === 401) {
    return reason === 'session_expired'
      ? { state: 'expired', reason }
      : { state: 'anonymous', reason: reason ?? 'not_authenticated' };
  }
  if (status === 403) return { state: 'forbidden', reason: reason ?? 'not_a_member' };
  if (status === 503) return { state: 'auth_unavailable', reason: reason ?? 'auth_unavailable' };
  if (status === 404) return { state: 'not_implemented' };
  return { state: 'unavailable', reason: reason ?? `http_${status ?? 0}` };
}

// 会话探测：同源携带 Cookie；网络失败 = 服务不可达（区别于 503 auth_unavailable）。
export async function fetchSession(fetchImpl = (typeof fetch !== 'undefined' ? fetch : null)) {
  if (!fetchImpl) return { state: 'unavailable', reason: 'no_fetch' };
  let res;
  try {
    res = await fetchImpl(sessionUrl(), { credentials: 'same-origin' });
  } catch {
    return { state: 'unavailable', reason: 'network' };
  }
  let body = null;
  try {
    body = await res.json();
  } catch { /* non-json */ }
  return classifySession({ status: res.status, body });
}

// ---- console_v3 只读联调（当前唯一真实后端 HTTP 查询；数据自带 shadow/fixture 标签） ----

export const V3_DEFAULT_BASE = 'http://127.0.0.1:4190';

export function v3RunsUrl(base = V3_DEFAULT_BASE) {
  return `${base.replace(/\/$/, '')}/api/runs`;
}

export function v3RunUrl(runId, base = V3_DEFAULT_BASE) {
  return `${base.replace(/\/$/, '')}/api/runs/${encodeURIComponent(runId)}`;
}

// v3 run 行 → pr-model 聚合输入。
// 红线：mode（shadow/fixture）原样保留在 _v3.mode，永不标为真实运行；
// v3 只读模型没有独立审查结论字段——review.verdict 保持 null，不伪造结论。
export function v3RunToRecord(r) {
  return {
    run_id: r.run_id,
    repo: r.repo,
    pr_number: r.pr_number,
    head_sha: r.head_sha ?? null,
    created_at: r.updated_at ?? null,
    execution: { status: r.superseded ? 'SUPERSEDED' : 'COMPLETED', source: 'console_v3 (read-only)' },
    review: { verdict: null },
    _v3: {
      mode: r.mode ?? null,
      outcome: r.outcome ?? null,
      risk_tier: r.risk_tier ?? null,
      review_complete: r.review_complete ?? null,
      coverage_missing: r.coverage_missing ?? [],
      superseded: Boolean(r.superseded),
    },
  };
}
