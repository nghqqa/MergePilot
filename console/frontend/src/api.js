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
    err.status = res.status; // 401=会话过期 403=无权限 404=不存在/未实现 —— 调用方分开处理
    throw err;
  }
  return body;
}

export const api = {
  health: () => get('/api/health'),
  // 会话探测：后端当前未实现（404）。401/403 语义为将来接入后端认证时区分（C-8 提案）。
  session: () => get('/api/session'),
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
