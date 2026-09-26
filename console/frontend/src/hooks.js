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
