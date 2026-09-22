// API client — all failures surface as {error} with honest messages.

async function get(path) {
  let res;
  try {
    res = await fetch(path);
  } catch (e) {
    throw new Error(`无法连接控制台后端（${e.message}）— 请确认 server.mjs 正在运行`);
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* non-json (e.g. static) */
  }
  if (!res.ok) {
    throw new Error(body?.error?.message ?? `HTTP ${res.status}`);
  }
  return body;
}

export const api = {
  health: () => get('/api/health'),
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
