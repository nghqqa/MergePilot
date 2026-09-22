import React, { useEffect, useState } from 'react';
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Activity, Database, FolderGit2, Hand, History, LogOut, Settings } from 'lucide-react';
import { api } from './api.js';
import { AuthProvider, useAuth } from './auth.jsx';
import { BrandMark, ErrorBoundary } from './ui.jsx';
import RunsPage from './pages/RunsPage.jsx';
import RunDetailPage from './pages/RunDetailPage.jsx';
import ReposPage from './pages/ReposPage.jsx';
import RepoPrsPage from './pages/RepoPrsPage.jsx';
import PrDetailPage from './pages/PrDetailPage.jsx';
import PendingPage from './pages/PendingPage.jsx';
import KnowledgePage from './pages/KnowledgePage.jsx';
import SettingsPage from './pages/SettingsPage.jsx';
import LoginPage from './pages/LoginPage.jsx';
import ApprovalsPage from './pages/ApprovalsPage.jsx';

const NAV = [
  { to: '/repos', label: '仓库', icon: FolderGit2, end: true },
  { to: '/pending', label: '待处理', icon: Hand, end: false },
  { to: '/knowledge', label: '知识库', icon: Database, end: true },
  { to: '/runs', label: '运行历史', icon: History, end: false },
  { to: '/settings', label: '设置', icon: Settings, end: true },
];

function TopbarContext() {
  // 数据模式 + 采集范围：静态事实标注，完整说明见设置页
  const [range, setRange] = useState(null);
  useEffect(() => {
    api.runs({ limit: 200 }).then((d) => {
      const times = (d.items ?? []).map((r) => r.created_at).filter(Boolean).sort();
      if (times.length) {
        const short = (iso) => iso.slice(0, 10);
        setRange(`${short(times[0])} ~ ${short(times[times.length - 1])}`);
      }
    }).catch(() => {});
  }, []);
  return (
    <span className="mode-wrap">
      <span className="mode-chip" title="数据模式 snapshot：全部数据来自仓库内锁定的真实历史运行证据包（SHA256SUMS 校验、只读）。live 实时模式未接入；服务仅监听 127.0.0.1 回环地址，无写操作接口，不下发凭证。">
        <span className="mode-dot" aria-hidden />
        <strong>历史快照 · 只读</strong>
      </span>
      {range ? <span className="mode-range">数据采集于 {range}</span> : null}
    </span>
  );
}

function AuthChip() {
  const auth = useAuth();
  if (auth.status === 'authed') {
    return (
      <span className="auth-chip">
        <span className="auth-user">{auth.user?.name ?? auth.user?.login ?? '已登录'}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={auth.refresh} title="登出需后端会话接口（C-8 提案）">
          <LogOut size={12} strokeWidth={1.75} aria-hidden /> 退出
        </button>
      </span>
    );
  }
  return (
    <span className="auth-chip">
      <span className="chip auth-demo-chip" title="只读演示预览：未认证浏览，使用现有脱敏历史快照；非登录态。">
        只读演示预览 · 未认证
      </span>
      <button type="button" className="btn btn-ghost btn-sm" onClick={auth.exitDemo} title="退出演示预览，返回登录页">
        <LogOut size={12} strokeWidth={1.75} aria-hidden /> 退出
      </button>
    </span>
  );
}

// 仓库上下文在顶栏持续可见：标题、面包屑、切换入口
function topbarCtx(pathname) {
  let seg = [];
  try {
    seg = pathname.split('/').filter(Boolean).map((s) => decodeURIComponent(s));
  } catch {
    seg = pathname.split('/').filter(Boolean);
  }
  if (seg[0] === 'repos' && seg.length >= 3) {
    const repo = `${seg[1]}/${seg[2]}`;
    if (seg[3] === 'pr' && seg[4]) {
      return { crumb: [['仓库', '/repos'], [repo, `/repos/${seg[1]}/${seg[2]}`]], current: `PR #${seg[4]}` };
    }
    return { crumb: [['仓库', '/repos']], current: repo };
  }
  if (seg[0] === 'runs' && seg.length > 1) {
    return { crumb: [['运行历史', '/runs']], current: '运行详情' };
  }
  const hit = NAV.find((n) => pathname === n.to || (n.end === false && pathname.startsWith(n.to)));
  return { crumb: null, current: hit?.label ?? '控制台' };
}

function Shell() {
  const loc = useLocation();
  const ctx = topbarCtx(loc.pathname);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <BrandMark />
          <div className="brand-text">
            <div className="brand-name">MergePilot</div>
            <div className="brand-sub">管理控制台 <span className="brand-v0">V0</span></div>
          </div>
        </div>
        <nav aria-label="主导航">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}
            >
              <n.icon size={15} strokeWidth={1.75} aria-hidden />
              <span className="nav-label">{n.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="foot-row" title="本服务仅监听 127.0.0.1 回环地址；不下发任何凭证">
            <Activity size={12} strokeWidth={1.75} aria-hidden /> 只读 · 无写操作 · 无凭证下发
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar-context">
            {ctx.crumb ? (
              <span className="crumb">
                {ctx.crumb.map(([label, to]) => (
                  <span key={to}>
                    <NavLink to={to}>{label}</NavLink>
                    <span className="crumb-sep">/</span>
                  </span>
                ))}
                <span className="crumb-current">{ctx.current}</span>
              </span>
            ) : (
              <span className="crumb-current">{ctx.current}</span>
            )}
          </div>
          <div className="topbar-right">
            <TopbarContext />
            <AuthChip />
          </div>
        </header>
        <main className="content" key={loc.pathname}>
          <ErrorBoundary>
            <Routes>
            <Route path="/" element={<Navigate to="/repos" replace />} />
            <Route path="/repos" element={<ReposPage />} />
            <Route path="/repos/:owner/:name" element={<RepoPrsPage />} />
            <Route path="/repos/:owner/:name/pr/:prNumber" element={<PrDetailPage />} />
            <Route path="/pending" element={<PendingPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:packId" element={<RunDetailPage />} />
            <Route path="/approvals" element={<ApprovalsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/login" element={<Navigate to="/repos" replace />} />
            <Route path="/rag" element={<Navigate to="/knowledge" replace />} />
            <Route path="/skills" element={<Navigate to="/knowledge" replace />} />
            <Route path="/usage" element={<Navigate to="/knowledge" replace />} />
            <Route path="*" element={<Navigate to="/repos" replace />} />
            </Routes>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Guarded />
    </AuthProvider>
  );
}

// 路由守卫：只改善交互（未认证先见登录页），不代替后端授权。
// 服务不可用与未登录分开呈现（见 LoginPage），不无限跳转。
function Guarded() {
  const auth = useAuth();
  if (auth.status === 'checking') {
    return (
      <div className="login-wrap" role="status">
        <div className="login-card"><p className="login-note">正在检查会话…</p></div>
      </div>
    );
  }
  if (!auth.admitted) return <LoginPage />;
  return <Shell />;
}
