import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { api } from './api.js';

// 会话交互结构（批次 B）。
// 边界：本组件只做交互状态机与守卫体验，不实现也不伪造认证；
// 身份与仓库权限由后端提供（INTEGRATION-REQUESTS C-8 提案，当前 GET /api/session=404）。
// 只读演示预览 = 明确标注的未认证浏览方式（sessionStorage，非登录、非长期凭证）。
const DEMO_KEY = 'mp-console-demo-preview';

export function readDemoPreview() {
  try {
    return sessionStorage.getItem(DEMO_KEY) === '1';
  } catch {
    return false;
  }
}

const AuthCtx = createContext(null);

export function AuthProvider({ children }) {
  // status: checking | anonymous | authed | expired | unavailable
  const [status, setStatus] = useState('checking');
  const [user, setUser] = useState(null);
  const [sessionSupported, setSessionSupported] = useState(false);
  const [demo, setDemo] = useState(readDemoPreview);

  const refresh = useCallback(async () => {
    setStatus('checking');
    try {
      await api.health();
    } catch {
      setStatus('unavailable');
      return;
    }
    try {
      const s = await api.session();
      setSessionSupported(true);
      if (s?.user) {
        setUser(s.user);
        setStatus('authed');
      } else {
        setUser(null);
        setStatus('anonymous');
      }
    } catch (e) {
      if (e.status === 401) {
        setSessionSupported(true);
        setUser(null);
        setStatus('expired');
        return;
      }
      // 404/501：后端未提供会话接口（现状）；403：已登录但无权限，按匿名呈现
      setSessionSupported(false);
      setUser(null);
      setStatus('anonymous');
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const enterDemo = useCallback(() => {
    try {
      sessionStorage.setItem(DEMO_KEY, '1');
    } catch { /* 隐私模式等 — 内存态即可 */ }
    setDemo(true);
  }, []);

  const exitDemo = useCallback(() => {
    try {
      sessionStorage.removeItem(DEMO_KEY);
    } catch { /* ignore */ }
    setDemo(false);
  }, []);

  const value = useMemo(() => ({
    status, user, sessionSupported, demo, refresh, enterDemo, exitDemo,
    // 是否放行浏览：后端已认证，或用户显式进入只读演示预览
    admitted: status === 'authed' || (demo && status !== 'checking' && status !== 'unavailable'),
  }), [status, user, sessionSupported, demo, refresh, enterDemo, exitDemo]);

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export function useAuth() {
  return useContext(AuthCtx);
}
