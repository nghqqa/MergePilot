import React, { createContext, useContext, useEffect, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Activity, History, LogOut } from 'lucide-react';
import { Layout, Menu as AntMenu, Drawer, Button } from 'antd';
import {
  InboxOutlined, FolderOpenOutlined, HistoryOutlined, AuditOutlined,
  DashboardOutlined, DatabaseOutlined, ApiOutlined, MedicineBoxOutlined,
  SettingOutlined, MenuOutlined, AppstoreOutlined, SafetyCertificateOutlined, FileSearchOutlined,
  TeamOutlined,
} from '@ant-design/icons';
import { api } from './api.js';
import { readCsrfCookie } from './api-live.js';
import { AuthProvider, useAuth } from './auth.jsx';
import { BrandMark, ErrorBoundary } from './ui.jsx';
import { useDataSource, useRuntimeConfig } from './hooks.js';
import { resolveWorkspaceState, WorkspacePanel } from './components/WorkspaceStatusPanel.jsx';
import RunsPage from './pages/RunsPage.jsx';
import RunDetailPage from './pages/RunDetailPage.jsx';
import DataSourcesPage from './pages/DataSourcesPage.jsx';
import CChainPage from './pages/CChainPage.jsx';
import RagTrialPage from './pages/RagTrialPage.jsx';
import MultiUserPage from './pages/MultiUserPage.jsx';
import DiagnosticsPage from './pages/DiagnosticsPage.jsx';
import ReposPage from './pages/ReposPage.jsx';
import RepoPrsPage from './pages/RepoPrsPage.jsx';
import PrDetailPage from './pages/PrDetailPage.jsx';
import PendingPage from './pages/PendingPage.jsx';
import CorePage from './pages/CorePage.jsx';
import OverviewPage from './pages/OverviewPage.jsx';
import KnowledgePage from './pages/KnowledgePage.jsx';
import SettingsPage from './pages/SettingsPage.jsx';
import LoginPage from './pages/LoginPage.jsx';
import ApprovalsPage from './pages/ApprovalsPage.jsx';

// 可信运行配置上下文：数据源模式由提供服务的后端声明（/api/health），
// sessionStorage/URL 无权改变（见 data/config.js）。
const ConfigCtx = createContext(null);
export function useAppConfig() {
  return useContext(ConfigCtx);
}

function Configured() {
  const { config, reload } = useRuntimeConfig();
  if (!config) {
    return (
      <div className="login-wrap" role="status">
        <div className="login-card"><p className="login-note">正在读取服务配置…</p></div>
      </div>
    );
  }
  // reloadConfig 挂在配置对象上（工作区面板"重试"用）；其余消费方仍按字段读取
  const ctx = Object.assign({}, config, { reloadConfig: () => reload(true) });
  return (
    <ConfigCtx.Provider value={ctx}>
      <Guarded />
    </ConfigCtx.Provider>
  );
}

// 审查工作台主导航：待处理（默认队列）→ 仓库 → 运行 → 审批
// （命名契约：/approvals=审批（人工放行动作）；审计=skill_gate_audit 等记录留痕，见"诊断"与 /core 的 Gate 审计区）
const NAV = [
  { to: '/overview', label: '运营总览', icon: AppstoreOutlined, end: true },
  { to: '/pending', label: '待处理', icon: InboxOutlined, end: false },
  { to: '/repos', label: '仓库', icon: FolderOpenOutlined, end: true },
  { to: '/runs', label: '运行记录', icon: HistoryOutlined, end: false },
  { to: '/approvals', label: '审批', icon: AuditOutlined, end: true },
];
// 系统区（非首要工作流）：系统状态与接线 / 知识库 / 数据源 / 诊断 / 设置
const SYSTEM_NAV = [
  { to: '/core', label: '系统状态', icon: DashboardOutlined, end: true },
  { to: '/knowledge', label: '知识库', icon: DatabaseOutlined, end: true },
  { to: '/datasources', label: '数据源', icon: ApiOutlined, end: true },
  { to: '/cchain', label: '签名验证', icon: SafetyCertificateOutlined, end: true },
  { to: '/rag-trial', label: '知识检索（试用）', icon: FileSearchOutlined, end: true },
  { to: '/multiuser', label: '组织与接入', icon: TeamOutlined, end: true },
  { to: '/diagnostics', label: '诊断', icon: MedicineBoxOutlined, end: true },
  { to: '/settings', label: '设置', icon: SettingOutlined, end: true },
];

function TopbarContext() {
  const config = useAppConfig();
  const auth = useAuth();
  const { source } = useDataSource(config);
  const [open, setOpen] = useState(false);
  const state = resolveWorkspaceState(config, auth.status);
  const toneCls = `ws-tone-${state.tone}`;
  const snapshot = source.kind === 'snapshot';

  return (
    <span className="ws-wrap">
      <button
        type="button"
        className={`mode-chip ${state.tone === 'bad' ? 'mode-chip-bad' : ''}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
        title="工作区状态——点击展开数据来源、只读/写操作、身份与接线详情"
      >
        <span className="mode-dot" aria-hidden />
        <strong>{state.label}</strong>
      </button>
      {open ? (
        <div className="ws-pop" role="dialog" aria-label="工作区状态详情">
          <WorkspacePanel
            config={config}
            auth={auth}
            onRetry={() => { auth.refresh(); }}
          />
          <div className="ws-pop-foot">
            {snapshot ? <span className="ws-sub">运行级全量历史见"运行"页。</span> : (
              <Link to="/runs" className="ws-sub">运行历史为 snapshot 取证视图（当前源不提供）。</Link>
            )}
            <button type="button" className="btn btn-sm" onClick={() => setOpen(false)}>收起</button>
          </div>
        </div>
      ) : null}
    </span>
  );
}

function AuthChip() {
  const auth = useAuth();
  const config = useAppConfig();
  const [logoutHint, setLogoutHint] = useState(null);
  // 登出（与设置页同一契约）：POST /api/auth/logout + X-CSRF-Token（mp_csrf Cookie）；
  // 完成后刷新会话，以服务端状态为准。失败（如 CSRF 缺失 403）时给出明确中文提示；
  // 刷新后会话若仍有效则如实保持登录——不伪造退出成功。
  const doLogout = async () => {
    setLogoutHint(null);
    let ok = false;
    let code = null;
    try {
      const res = await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRF-Token': readCsrfCookie() ?? '' },
      });
      ok = res.ok;
      code = res.status;
    } catch (e) {
      code = `网络错误：${String(e?.message ?? '').slice(0, 40)}`;
    }
    if (!ok) {
      setLogoutHint(code === 403
        ? '退出未生效（403：CSRF 校验失败或会话已变化）——以服务端会话为准，请重试或用"设置"页退出'
        : `退出未生效（${code ?? '未知'}）——以服务端会话为准，请重试`);
    }
    auth.refresh();
  };
  // fixture 验收环境（可信配置声明 data_mode=fixture）：会话即合成用户，标识常驻可见
  const fixtureSession = config?.dataMode === 'fixture' && auth.status === 'authed';
  // console-pg 联调环境：认证未实现（401）——页面必须显示未认证/fixture 状态
  const pgUnauthed = config?.mode === 'console-pg' && auth.status !== 'authed';
  if (auth.status === 'authed') {
    return (
      <span className="auth-chip">
        {fixtureSession ? (
          <span className="chip auth-demo-chip" title="Fixture 验收会话：合成用户（非真实 GitHub 身份），数据为合成 fixture——仅开发/测试环境使用">
            Fixture 验收会话 · 非真实
          </span>
        ) : (
          <span className="auth-user">{auth.user?.display_name ?? auth.user?.github_login ?? auth.user?.name ?? '已登录'}</span>
        )}
        <button type="button" className="btn btn-ghost btn-sm" onClick={doLogout}
          title="结束服务端会话（POST /api/auth/logout，携带 CSRF）；退出后需重新登录">
          <LogOut size={12} strokeWidth={1.75} aria-hidden /> 退出登录
        </button>
        {logoutHint ? <span className="auth-logout-hint" role="alert" title={logoutHint}>{logoutHint}</span> : null}
      </span>
    );
  }
  if (pgUnauthed) {
    return (
      <span className="auth-chip">
        <span className="chip auth-demo-chip" title="隔离 PG fixture 服务：认证未实现（GET /api/auth/session → 401 not_authenticated）——未认证状态，非真实用户会话。">
          PG Fixture · 未认证
        </span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={auth.refresh} title="重新探测会话端点">
          重查会话
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
  const all = [...NAV, ...SYSTEM_NAV];
  const hit = all.find((n) => pathname === n.to
    || (n.to !== '/' && pathname.startsWith(n.to + '/'))
    || (n.end === false && pathname.startsWith(n.to)));
  return { crumb: null, current: hit?.label ?? '控制台' };
}

function Shell() {
  const loc = useLocation();
  const config = useAppConfig();
  const { source } = useDataSource(config);
  const ctx = topbarCtx(loc.pathname);
  const [navOpen, setNavOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(
    typeof window !== 'undefined' && window.matchMedia('(max-width: 960px)').matches);
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 960px)');
    const on = (e) => setIsMobile(e.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  useEffect(() => { setNavOpen(false); }, [loc.pathname]);

  // 导航按数据源能力呈现：run 级全量历史仅 snapshot 取证视图提供
  const nav = NAV.filter((n) => !(source.kind !== 'snapshot' && n.to === '/runs'));

  const menuItems = [
    ...nav.map((n) => ({ key: n.to, icon: <n.icon />, label: <NavLink to={n.to}>{n.label}</NavLink> })),
    { type: 'group', label: '系统', children: SYSTEM_NAV.map((n) => ({
      key: n.to, icon: <n.icon />, label: <NavLink to={n.to}>{n.label}</NavLink> })) },
  ];
  const selectedKey = [...nav, ...SYSTEM_NAV]
    .filter((n) => loc.pathname === n.to || (n.end === false && loc.pathname.startsWith(n.to)))
    .map((n) => n.to);

  const brand = (
    <div className="brand">
      <BrandMark />
      <div className="brand-text">
        <div className="brand-name">MergePilot</div>
        <div className="brand-sub">审查工作台 <span className="brand-v0">V0</span></div>
      </div>
    </div>
  );
  const menu = (
    // R4（FB-07）：focusable=false 移除 rc-menu 对 UL 的 tabindex=0 吸收——
    // Tab 不再被困在菜单容器上，可顺次进入各菜单项内的真实链接（有 href）并离开到主内容。
    <AntMenu theme="dark" mode="inline" items={menuItems} selectedKeys={selectedKey} focusable={false} />
  );

  return (
    <Layout className="app" style={{ minHeight: '100vh' }}>
      {!isMobile ? (
        <Layout.Sider width={216} className="sidebar" breakpoint={false}>
          {brand}
          <nav aria-label="主导航">{menu}</nav>
          <div className="sidebar-foot">
            <div className="foot-row" title="本服务仅监听 127.0.0.1 回环地址；不下发任何凭证">
              <Activity size={12} strokeWidth={1.75} aria-hidden />
              只读 · 无写操作 · 无凭证下发
            </div>
          </div>
        </Layout.Sider>
      ) : (
        <Drawer
          placement="left" width={232} open={navOpen} onClose={() => setNavOpen(false)}
          title={brand} styles={{ body: { padding: 0, background: '#0b0f19' } }}
          closeIcon={<MenuOutlined aria-label="关闭菜单" />}
        >
          <nav aria-label="主导航">{menu}</nav>
        </Drawer>
      )}
      <Layout>
        <header className="topbar">
          {isMobile ? (
            <Button type="text" aria-expanded={navOpen} aria-label="打开菜单"
                    icon={<MenuOutlined />} onClick={() => setNavOpen(true)}
                    style={{ marginRight: 8 }} />
          ) : null}
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
              <Route path="/" element={<Navigate to="/overview" replace />} />
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="/core" element={<CorePage />} />
              <Route path="/datasources" element={<DataSourcesPage />} />
              <Route path="/cchain" element={<CChainPage />} />
              <Route path="/rag-trial" element={<RagTrialPage />} />
              <Route path="/multiuser" element={<MultiUserPage />} />
              <Route path="/diagnostics" element={<DiagnosticsPage />} />
              <Route path="/repos" element={<ReposPage />} />
              <Route path="/repos/:owner/:name" element={<RepoPrsPage />} />
              <Route path="/repos/:owner/:name/pr/:prNumber" element={<PrDetailPage />} />
              <Route path="/pending" element={<PendingPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/runs" element={<RunsHistoryRoute />} />
              <Route path="/runs/:packId" element={<RunDetailPage />} />
              <Route path="/approvals" element={<ApprovalsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/login" element={<Navigate to="/pending" replace />} />
              <Route path="/rag" element={<Navigate to="/knowledge" replace />} />
              <Route path="/skills" element={<Navigate to="/knowledge" replace />} />
              <Route path="/usage" element={<Navigate to="/knowledge" replace />} />
              <Route path="*" element={<Navigate to="/pending" replace />} />
            </Routes>
          </ErrorBoundary>
        </main>
      </Layout>
    </Layout>
  );
}

function RunsHistoryRoute() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  if (source.kind !== 'snapshot') {
    return (
      <div className="state-box state-warn" role="status">
        运行历史为 snapshot 取证视图——当前数据源（{source.kind}）不提供 run 级全量历史。
        PR 维度历史在各 PR 详情的"运行历史"展开中查看。返回<Link to="/repos">仓库工作台</Link>。
      </div>
    );
  }
  return <RunsPage />;
}

export default function App() {
  return (
    <AuthProvider>
      <Configured />
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
