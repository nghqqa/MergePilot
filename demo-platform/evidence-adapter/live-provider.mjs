// evidence-adapter/live-provider.mjs
// Live provider — READ-ONLY probes against the real AgentTeams runtime + GitHub.
// Honest by construction: a source that is down/unconfigured is reported as
// unavailable; nothing is ever faked. Credentials (if any) come from env and are
// used server-side only — never echoed in responses.

import { EVIDENCE_DIRS } from './evidence.mjs';

const env = process.env;
const TIMEOUT_MS = Number(env.DEMO_LIVE_TIMEOUT_MS || 3000);

const SOURCES = {
  matrix: {
    label: 'Matrix (Tuwunel homeserver)',
    url: env.DEMO_LIVE_MATRIX_URL || 'http://127.0.0.1:6167/_matrix/client/versions',
    core: true,
  },
  element: {
    label: 'Element Web',
    url: env.DEMO_LIVE_ELEMENT_URL || 'http://127.0.0.1:18088/config.json',
    core: false,
  },
  minio: {
    label: 'MinIO',
    url: env.DEMO_LIVE_MINIO_URL || 'http://127.0.0.1:9000/minio/health/live',
    core: true,
  },
  controller: {
    label: 'AgentTeams Controller API',
    url: env.DEMO_LIVE_CONTROLLER_URL || '', // not configured by default — honest
    core: true,
    requires_token: true,
  },
  github: {
    label: 'GitHub PR metadata (unauthenticated, read-only)',
    url: 'https://api.github.com/repos/nghqqa/fastapi-boilerplate-demo/pulls/2',
    core: false,
  },
};

async function probe(url, { headers = {} } = {}) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: ctl.signal, headers });
    const text = await res.text();
    return { ok: res.ok, status: res.status, head: text.slice(0, 400) };
  } catch (e) {
    return { ok: false, status: null, error: e.name === 'AbortError' ? `timeout after ${TIMEOUT_MS}ms` : String(e && e.message || e) };
  } finally {
    clearTimeout(t);
  }
}

let lastSnapshot = null;
let inflight = null;
const CACHE_MS = 15000;

export async function liveStatus({ refresh = false } = {}) {
  const now = Date.now();
  if (!refresh && lastSnapshot && now - lastSnapshot.checked_at_ms < CACHE_MS) return lastSnapshot;
  if (inflight) return inflight;
  inflight = (async () => {
    const sources = {};
    for (const [key, s] of Object.entries(SOURCES)) {
      if (!s.url) {
        sources[key] = { label: s.label, configured: false, status: 'NOT_CONFIGURED', detail: '设置 DEMO_LIVE_CONTROLLER_URL（及可选 DEMO_LIVE_CONTROLLER_TOKEN）后启用；凭据仅保存在服务端环境变量' };
        continue;
      }
      const headers = {};
      if (s.requires_token && env.DEMO_LIVE_CONTROLLER_TOKEN) headers.authorization = `Bearer ${env.DEMO_LIVE_CONTROLLER_TOKEN}`;
      const r = await probe(s.url, { headers });
      sources[key] = {
        label: s.label,
        configured: true,
        status: r.ok ? 'OK' : 'UNAVAILABLE',
        http_status: r.status ?? null,
        detail: r.ok ? 'read-only probe succeeded' : (r.error || `HTTP ${r.status}`),
        // never echo response head for auth-bearing endpoints
      };
    }
    const coreOk = Object.entries(SOURCES).filter(([, s]) => s.core).map(([k]) => sources[k]).every((x) => x?.status === 'OK' || x?.status === 'NOT_CONFIGURED' ? x?.status === 'OK' : false);
    const anyCoreOk = Object.entries(SOURCES).filter(([, s]) => s.core).some(([k]) => sources[k]?.status === 'OK');
    const snapshot = {
      provider: 'live',
      banner: 'LIVE — CONNECTED TO AGENTTEAMS',
      unavailable_banner: 'LIVE DATA UNAVAILABLE',
      checked_at: new Date().toISOString(),
      checked_at_ms: now,
      sources,
      available: anyCoreOk,
      fully_connected: coreOk,
      note: anyCoreOk
        ? '至少一个核心 AgentTeams 数据源可达；未配置/不可达的数据源已逐项如实标注，未用任何假数据补齐'
        : '核心 AgentTeams 数据源（Matrix/MinIO/Controller）均不可达 — 本机 AgentTeams runtime 当前未运行。请启动 p14h2-wd runtime 或配置 DEMO_LIVE_* 环境变量；在此之前 Live 模式显示 LIVE DATA UNAVAILABLE，不回退伪造数据',
      // TRUTHFULNESS-FIX: never infer LIVE from local evidence-replay files.
      // Phase 14.2H: live cloud trace confirmed by operator in the Aliyun console
      // (trace fbf4a3cec0493990d76e10a102418be1) + local relay/span corroboration.
      // Verdict stays operator-level — no programmatic cloud-query record yet.
      trace: {
        status: 'SMOKE_VERIFIED_LIVE_CLOUD_CONFIRMED_BY_OPERATOR',
        smoke: 'VERIFIED (Aliyun AgentLoop OTLP smoke)',
        live_copaw_run: 'VERIFIED (LIVE CLOUD, operator-confirmed: fbf4a3cec0493990d76e10a102418be1)',
        note: '真实 CoPaw run 合并 Trace（Agent+LLM+Tool）已由操作员在阿里云控制台确认；verdict=AGENTLOOP_ALIYUN_TRACE_VISIBILITY_CONFIRMED_BY_OPERATOR，不声称 6/6 稳定覆盖',
      },
      rag: { status: 'NOT_IMPLEMENTED', detail: 'PolarDB RAG 未接入' },
    };
    lastSnapshot = snapshot;
    inflight = null;
    return snapshot;
  })();
  return inflight;
}

// Live PR metadata from GitHub (public, read-only, unauthenticated).
export async function livePrState(number) {
  const url = `https://api.github.com/repos/nghqqa/fastapi-boilerplate-demo/pulls/${number}`;
  try {
    const res = await fetch(url, { headers: { 'User-Agent': 'mergepilot-demo-platform' }, signal: AbortSignal.timeout(TIMEOUT_MS) });
    if (!res.ok) {
      return { available: false, error: `HTTP ${res.status}`, note: 'GitHub API 不可用（可能被限流）— 如实显示不可用，不伪造' };
    }
    const j = await res.json();
    return {
      available: true,
      number: j.number,
      state: j.state,
      merged: !!j.merged,
      title: j.title,
      head_branch: j.head && j.head.ref,
      updated_at: j.updated_at,
      url: j.html_url,
      source: url,
    };
  } catch (e) {
    return { available: false, error: e.name === 'TimeoutError' ? 'timeout' : String(e && e.message || e) };
  }
}

// Live approval target — the ONLY write path in live mode. Currently unconfigured
// by default: writing to the real runtime requires an explicitly configured
// protected endpoint. Until then we refuse rather than fake.
export function liveApprovalTarget() {
  const url = env.DEMO_LIVE_APPROVAL_URL || '';
  return {
    configured: !!url,
    url: url || null, // url itself is not a secret
    note: url
      ? 'Live 审批将调用受保护的 Demo Backend 审批接口（服务端持有凭据）'
      : '未配置 DEMO_LIVE_APPROVAL_URL — Live 模式审批接口不可用，如实返回 LIVE_DATA_UNAVAILABLE，绝不伪装成功',
  };
}
