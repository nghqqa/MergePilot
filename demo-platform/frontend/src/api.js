// frontend/src/api.js — thin fetch layer over the Demo Backend (the ONLY data path;
// the frontend never touches Matrix/MinIO/controller credentials).

const BASE = import.meta.env.DEV ? '' : ''; // same origin in prod & dev(proxy)

export class ApiErr extends Error {
  constructor(status, payload) {
    super(payload?.error || `HTTP ${status}`);
    this.status = status;
    this.payload = payload;
  }
}

export async function api(path, { method = 'GET', body } = {}) {
  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method,
      headers: body ? { 'content-type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new ApiErr(0, { error: 'NETWORK_ERROR', detail: 'Demo Backend 不可达 — 请确认后端已启动 (node backend/server.mjs)' });
  }
  let json = null;
  try {
    json = await res.json();
  } catch {
    /* non-json */
  }
  if (!res.ok) throw new ApiErr(res.status, json || { error: `HTTP ${res.status}` });
  return json;
}
