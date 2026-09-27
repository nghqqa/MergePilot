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

async function post(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    });
  } catch (e) {
    const err = new Error(`无法连接控制台后端（${e.message}）— 请确认 server.mjs 正在运行`);
    err.code = 'NETWORK';
    throw err;
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-json */ }
  if (!res.ok) {
    const err = new Error(data?.error ?? `HTTP ${res.status}`);
    err.status = res.status;
    err.reason = data?.error_kind ?? null;
    throw err;
  }
  return data;
}

export const api = {
  health: () => get('/api/health'),
  // RAG 本地试验（LOCAL_RAG_TRIAL；与 A 链 org-search / C 链 cchain 并行隔离）
  ragTrialStatus: () => get('/api/rag-trial/status'),
  ragTrialMetrics: () => get('/api/rag-trial/metrics'),
  ragTrialQuery: (body) => post('/api/rag-trial/query', body),
  ragTrialIngest: (body) => post('/api/rag-trial/ingest', body),
  ragTrialEval: (body) => post('/api/rag-trial/eval', body),
  ragTrialReviewAux: (body) => post('/api/rag-trial/review-aux', body),
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
  // C 链（cchain）真实状态/指标（B 轨接线；BLOCKED 即如实显示 BLOCKED）
  cchainStatus: () => get('/api/cchain/status'),
  cchainMetrics: () => get('/api/cchain/metrics'),
};
