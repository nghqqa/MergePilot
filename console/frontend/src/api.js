// API client — all failures surface as {error} with honest messages.

async function get(path) {
  let res;
  try {
    res = await fetch(path);
  } catch (e) {
    const err = new Error(`无法连接控制台后端（${e.message}）— 请确认 server.mjs 正在运行`);
    err.code = 'NETWORK';
    throw err;
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* non-json (e.g. static) */
  }
  if (!res.ok) {
    const err = new Error(body?.error?.message ?? `HTTP ${res.status}`);
    err.status = res.status;                    // 401=未登录/过期 403=无权限 404=不存在/未实现 503=服务不可用
    err.reason = body?.error?.reason ?? null;   // 契约 v2 machine code（not_authenticated/session_expired/…）
    throw err;
  }
  return body;
}

export const api = {
  health: () => get('/api/health'),
  // 会话：正式契约端点 GET /api/auth/session（未登录 401 JSON，不重定向；7ccecb9）。
  // 交互层请用 api-live.fetchSession（带状态分类）；此处保留通用 GET。
  session: () => get('/api/auth/session'),
  runs: (params = {}) => {
    const q = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== '' && v != null)
    ).toString();
    return get(`/api/runs${q ? `?${q}` : ''}`);
  },
  run: (packId) => get(`/api/runs/${encodeURIComponent(packId)}`),
  evidence: (packId) => get(`/api/runs/${encodeURIComponent(packId)}/evidence`),
  evidenceContent: (packId, path) =>
    get(`/api/runs/${encodeURIComponent(packId)}/evidence/content?path=${encodeURIComponent(path)}`),
  evidenceDownloadUrl: (packId, path) =>
    `/api/runs/${encodeURIComponent(packId)}/evidence/download?path=${encodeURIComponent(path)}`,
  integrity: (packId) => get(`/api/runs/${encodeURIComponent(packId)}/integrity`),
};
