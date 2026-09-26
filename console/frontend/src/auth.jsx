import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { fetchSession } from './api-live.js';

// 会话交互结构（契约 v2 @7ccecb9 对齐）。
// 正式端点：GET /api/auth/session（未登录 401 JSON，服务端不重定向）；GitHub OAuth + 服务端会话
// 方案已定——等待后端实现与 D-9 配置；控制台不自建账号库、不伪造登录成功。
// 会话权威 = 服务端 Cookie（mp_session）；浏览器不保存 token/私钥/长期凭证，不用 localStorage 缓存身份。
// 只读演示预览 = 明确标注的未认证浏览（sessionStorage 标记，非登录）：仅本地脱敏 snapshot 数据，
// 不发起 live API 调用、不因预览标记获得真实用户身份。
const DEMO_KEY = 'mp-console-demo-preview';

export function readDemoPreview() {
  try {
    return sessionStorage.getItem(DEMO_KEY) === '1';
  } catch {
    return false;
  }
}

const AuthCtx = createContext(null);

// 演示预览的放行状态集：登录服务不可用（auth_unavailable/网络不可达）时按契约不开放产品，也不放行预览。
const DEMO_BLOCKED = new Set(['checking', 'unavailable', 'auth_unavailable']);

export function AuthProvider({ children }) {
  // status: checking | anonymous | authed | expired | forbidden | auth_unavailable | not_implemented | unavailable
  const [session, setSession] = useState({ state: 'checking', user: null, reason: null, expiresAt: null });
  const [demo, setDemo] = useState(readDemoPreview);

  const refresh = useCallback(async () => {
    setSession({ state: 'checking', user: null, reason: null, expiresAt: null });
    const r = await fetchSession();
    setSession({
      state: r.state,
      user: r.user ?? null,
      reason: r.reason ?? null,
      expiresAt: r.expiresAt ?? null,
    });
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

  const status = session.state;
  const value = useMemo(() => ({
    status,
    user: session.user,
    expiresAt: session.expiresAt,
    reason: session.reason,
    demo,
    refresh,
    enterDemo,
    exitDemo,
    // 放行浏览：后端已认证，或用户显式进入只读演示预览（服务不可用/登录服务不可用时一律不放行）
    admitted: status === 'authed' || (demo && !DEMO_BLOCKED.has(status)),
  }), [status, session.user, session.expiresAt, session.reason, demo, refresh, enterDemo, exitDemo]);

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export function useAuth() {
  return useContext(AuthCtx);
}
