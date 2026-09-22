import { useCallback, useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api } from './api.js';

// 全量运行快照（≤200 条，loopback 单用户工具，够用且与现有页面一致）
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

// 返回列表时恢复滚动位置：按路由 entry key 存取（sessionStorage，随会话结束丢弃）
export function useScrollRestore() {
  const location = useLocation();
  useEffect(() => {
    const key = `mp-scroll:${location.key}`;
    let saved = null;
    try {
      saved = sessionStorage.getItem(key);
    } catch { /* ignore */ }
    if (saved) window.scrollTo(0, Number(saved) || 0);
    const onScroll = () => {
      try {
        sessionStorage.setItem(key, String(window.scrollY));
      } catch { /* ignore */ }
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, [location.key]);
}
