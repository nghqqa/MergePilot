import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api } from './api.js';
import { createDataSource } from './data/sources.js';
import { loadRuntimeConfig } from './data/config.js';

// 全量运行快照（snapshot 源专用：运行历史页/客户端聚合）
export function useAllRuns() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback(() => {
    setData(null);
    setError(null);
    api.runs({ limit: 200 }).then(setData).catch(setError);
  }, []);
  useEffect(() => {
    load();
  }, [load]);
  return { data, error, retry: load };
}

// 返回列表时恢复滚动位置：作用在真正的滚动容器 .content 上
// （页面级滚动发生在 .content，window 不滚动）
export function useScrollRestore() {
  const location = useLocation();
  useEffect(() => {
    const container = document.querySelector('.content');
    if (!container) return undefined;
    const key = `mp-scroll:${location.key}`;
    let saved = null;
    try {
      saved = sessionStorage.getItem(key);
    } catch { /* ignore */ }
    if (saved) container.scrollTop = Number(saved) || 0;
    const onScroll = () => {
      try {
        sessionStorage.setItem(key, String(container.scrollTop));
      } catch { /* ignore */ }
    };
    container.addEventListener('scroll', onScroll, { passive: true });
    return () => container.removeEventListener('scroll', onScroll);
  }, [location.key]);
}

// /api/runs 会话内共享缓存：顶栏与页面共用同一次请求，不重复打后端
let runsSnapshotPromise = null;
export function fetchRunsSnapshotOnce() {
  if (!runsSnapshotPromise) {
    runsSnapshotPromise = fetch('/api/runs?limit=200', { credentials: 'same-origin' })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .catch((e) => { runsSnapshotPromise = null; throw e; });
  }
  return runsSnapshotPromise;
}

// ---- 数据源注入（页面只经此消费数据；模式来自可信服务配置，客户端无权切换） ----

export function useRuntimeConfig() {
  const [config, setConfig] = useState(null);
  const [error, setError] = useState(null);
  const load = useCallback((force = false) => {
    loadRuntimeConfig(undefined, force).then(
      (c) => { setConfig(c); return c; },
      (e) => { setError(e); },
    );
  }, []);
  useEffect(() => { load(); }, [load]);
  return { config, error, reload: () => load(true) };
}

export function useDataSource(config) {
  return useMemo(
    () => (config ? { source: createDataSource(config), config } : { source: null, config }),
    [config],
  );
}

// 竞态守卫查询：deps 变化/卸载后，晚到的旧响应一律丢弃（不覆盖当前仓库页面）
export function useSourceQuery(queryFn, deps, { enabled = true } = {}) {
  const [state, setState] = useState({ status: enabled ? 'loading' : 'idle' });
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    if (!enabled) {
      setState({ status: 'idle' });
      return undefined;
    }
    setState({ status: 'loading' });
    Promise.resolve()
      .then(queryFn)
      .then((data) => { if (aliveRef.current) setState({ status: 'done', data }); })
      .catch((error) => { if (aliveRef.current) setState({ status: 'error', error }); });
    return () => { aliveRef.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

// ---- 顶栏账户摘要（组织/角色）----
// 数据可信度加固（PR #320 三波）：
//   · 缓存按会话身份键（login + org）失效——A 登出后 B 登录不残留 A 的组织/角色；
//   · 失败不缓存——首次请求失败可恢复（下次 enabled 变化/登入重试）；
//   · 请求代际令牌——旧会话的延迟响应不得覆盖新会话数据。
let muSummaryCache = { key: null, data: null };
let muSummaryToken = 0;

function accountCacheKey(sessionUser) {
  // 身份键：优先后端 user id/login（跨用户必不同）；同用户重复登录共享缓存无害
  return String(sessionUser?.github_login ?? sessionUser?.login ?? sessionUser?.name ?? '');
}

export function resetMuAccountSummaryCache() {
  muSummaryCache = { key: null, data: null };
  muSummaryToken += 1; // 使在途请求全部过期
}

export async function fetchMuAccountSummary(sessionUser) {
  const key = accountCacheKey(sessionUser);
  if (!key) return null;
  if (muSummaryCache.key === key && muSummaryCache.data) return muSummaryCache.data;
  const myToken = ++muSummaryToken;
  try {
    const r = await fetch('/api/mu/session', { credentials: 'same-origin' });
    const b = r.ok ? await r.json().catch(() => null) : null;
    const summary = (b?.user && b?.tenant) ? {
      login: b.user.login ?? b.user.name ?? '',
      org: b.tenant.slug ?? '',
      role: b.role ?? '',
    } : null;
    // 代际+身份双校验：响应到达时若已换会话/换键，丢弃（防旧会话延迟响应覆盖）
    if (myToken !== muSummaryToken || accountCacheKey(sessionUser) !== key) return null;
    if (summary) muSummaryCache = { key, data: summary };
    return summary;
  } catch {
    if (myToken !== muSummaryToken) return null;
    return null; // 失败不缓存——下次重试
  }
}

export function useMuAccountSummary(enabled, sessionUser) {
  const [summary, setSummary] = useState(null);
  const userKey = accountCacheKey(sessionUser);
  useEffect(() => {
    if (!enabled || !userKey) { setSummary(null); return undefined; }
    let alive = true;
    fetchMuAccountSummary(sessionUser).then((s) => { if (alive) setSummary(s); });
    return () => { alive = false; };
  }, [enabled, userKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return summary;
}
