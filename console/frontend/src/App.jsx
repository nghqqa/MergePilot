import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { Link, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Activity, History, LogOut, UserRound } from 'lucide-react';
import { Layout, Menu as AntMenu, Drawer, Button, Dropdown, Tag } from 'antd';
import {
  InboxOutlined, FolderOpenOutlined, HistoryOutlined, AuditOutlined,
  DashboardOutlined, DatabaseOutlined, ApiOutlined, MedicineBoxOutlined,
  SettingOutlined, MenuOutlined, AppstoreOutlined, SafetyCertificateOutlined, FileSearchOutlined,
  TeamOutlined, ToolOutlined, ExperimentOutlined,
} from '@ant-design/icons';
import { api } from './api.js';
import { readCsrfCookie } from './api-live.js';
import { AuthProvider, useAuth, DEMO_BLOCKED } from './auth.jsx';
import { BrandMark, ErrorBoundary } from './ui.jsx';
import { deriveIdentitySource, capabilityLine } from './identity.js';
import { useDataSource, useRuntimeConfig, useMuAccountSummary, resetMuAccountSummaryCache } from './hooks.js';
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
import { MuPrDetail } from './pages/MuPrDetail.jsx';
import PendingPage from './pages/PendingPage.jsx';
import CorePage from './pages/CorePage.jsx';
import OverviewPage from './pages/OverviewPage.jsx';
import KnowledgePage from './pages/KnowledgePage.jsx';
import SettingsPage from './pages/SettingsPage.jsx';
import LoginPage from './pages/LoginPage.jsx';
import ApprovalsPage from './pages/ApprovalsPage.jsx';
import SkillsPage from './pages/SkillsPage.jsx';

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

// 一级导航（审查工作台主流程，只保留四项——UX 收敛审查 2026-10-05）：
// 总览（审查工作台首屏）→ 待处理（队列）→ 仓库 → 审批（人工放行动作）。
//（命名契约：/approvals=审批（人工放行动作）；审计=skill_gate_audit 等记录留痕，见"诊断"与 /core 的 Gate 审计区）
const NAV = [
  { to: '/overview', label: '总览', icon: AppstoreOutlined, end: true },
  { to: '/pending', label: '待处理', icon: InboxOutlined, end: false },
  { to: '/repos', label: '仓库', icon: FolderOpenOutlined, end: true },
  { to: '/approvals', label: '审批', icon: AuditOutlined, end: true },
];
// 系统管理（部署/运营面，非主审查流程）：系统状态与接线 / 数据源 / 组织与接入 /
// 运行记录（snapshot 取证视图，实时源下隐藏）/ 设置
const SYSTEM_NAV = [
  { to: '/core', label: '系统状态', icon: DashboardOutlined, end: true },
  { to: '/datasources', label: '数据源', icon: ApiOutlined, end: true },
  { to: '/multiuser', label: '组织与接入', icon: TeamOutlined, end: true },
  { to: '/runs', label: '运行记录', icon: HistoryOutlined, end: false },
  { to: '/settings', label: '设置', icon: SettingOutlined, end: true },
];
// 高级工具（默认折叠——低频/专家向能力）：知识库 / 技能 / 签名验证 / 知识检索 / 诊断
const ADVANCED_NAV = [
  { to: '/knowledge', label: '知识库', icon: DatabaseOutlined, end: true },
  // B 波补遗：#290 只接了 /skills 路由，漏了导航入口（验收时误判槽位已存在——实测用户看不到页面）
  { to: '/skills', label: '技能', icon: ToolOutlined, end: true },
  { to: '/cchain', label: '签名验证', icon: SafetyCertificateOutlined, end: true },
  { to: '/rag-trial', label: '知识检索', icon: FileSearchOutlined, end: true },
  { to: '/diagnostics', label: '诊断', icon: MedicineBoxOutlined, end: true },
];
const ALL_NAV = [...NAV, ...SYSTEM_NAV, ...ADVANCED_NAV];

// MU 角色 → 中文标签（顶栏账户区；raw 值在 title 中保留）
const ROLE_LABELS = {
  platform_admin: '平台管理员', maintainer: '维护者', reviewer: '审查者',
  contributor: '贡献者', auditor: '审计',
};

function TopbarContext() {
  const config = useAppConfig();
  const auth = useAuth();
  const { source } = useDataSource(config);
  const [open, setOpen] = useState(false);
  const chipRef = useRef(null);
  const state = resolveWorkspaceState(config, auth.status);
  const toneCls = `ws-tone-${state.tone}`;
  void toneCls;
  const snapshot = source.kind === 'snapshot';

  // Esc 关闭弹层并把焦点还给触发 chip（dialog 语义配套；弹层外点击仍由收起钮/再次点击 chip 关闭）
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        setOpen(false);
        chipRef.current?.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open]);

  return (
    <span className="ws-wrap">
      <button
        type="button"
        ref={chipRef}
        className={`mode-chip ${state.tone === 'bad' ? 'mode-chip-bad' : ''}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((v) => !v)}
        title={`${state.full}——点击展开数据来源、只读/写操作、身份与接线详情`}
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
            {snapshot ? <span className="ws-sub">运行级全量历史见"运行记录"页。</span> : (
              <Link to="/runs" className="ws-sub">运行历史为 snapshot 取证视图（当前源不提供）。</Link>
            )}
            <button type="button" className="btn btn-sm" onClick={() => setOpen(false)}>收起</button>
          </div>
        </div>
      ) : null}
    </span>
  );
}

function AuthChip({ compact = false, summary = null }) {
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
    resetMuAccountSummaryCache(); // 换号防护：A 登出后 B 登录不残留 A 的组织/角色
    auth.refresh();
  };
  // fixture 验收环境（可信配置声明 data_mode=fixture）：会话即合成用户，标识常驻可见
  const fixtureSession = config?.dataMode === 'fixture' && auth.status === 'authed';
  // console-pg 联调环境：认证未实现（401）——页面必须显示未认证/fixture 状态
  const pgUnauthed = config?.mode === 'console-pg' && auth.status !== 'authed';

  const userName = auth.user?.display_name ?? auth.user?.github_login ?? auth.user?.name ?? '已登录';
  const roleLabel = summary?.role ? (ROLE_LABELS[summary.role] ?? summary.role) : null;

  // 小屏账户菜单：账号/组织/角色/退出全部收进 Dropdown（≤768px 顶栏不拥挤、不换行错位）
  if (compact && auth.status === 'authed') {
    return (
      <Dropdown
        trigger={['click']}
        menu={{
          items: [
            { key: 'who', label: <strong>{fixtureSession ? 'Fixture 验收会话（非真实）' : userName}</strong>, disabled: true },
            ...(summary ? [
              { key: 'org', label: <>组织：<span className="mono">{summary.org || '—'}</span></>, disabled: true },
              { key: 'role', label: <>角色：{roleLabel ? <Tag>{roleLabel}</Tag> : '—'}</>, disabled: true },
            ] : []),
            { type: 'divider' },
            { key: 'logout', icon: <LogOut size={13} aria-hidden />, label: '退出登录', onClick: doLogout },
          ],
        }}
      >
        <Button type="text" size="small" aria-label={`账户菜单：${fixtureSession ? 'Fixture 会话' : userName}，含组织、角色与退出登录`}
          className="account-trigger">
          <UserRound size={16} strokeWidth={1.75} aria-hidden />
        </Button>
      </Dropdown>
    );
  }

  if (auth.status === 'authed') {
    return (
      <span className="auth-chip">
        {fixtureSession ? (
          <span className="chip auth-demo-chip" title="Fixture 验收会话：合成用户（非真实 GitHub 身份），数据为合成 fixture——仅开发/测试环境使用">
            Fixture 验收会话 · 非真实
          </span>
        ) : (
          <>
            <span className="auth-user" title={`当前账号：${userName}`}>{userName}</span>
            {summary ? (
              <span className="topbar-org" title={`组织 ${summary.org || '—'} · 角色 ${roleLabel ?? summary.role}`}>
                <span className="muted">组织</span> <span className="mono">{summary.org || '—'}</span>
                {roleLabel ? <Tag style={{ marginInlineStart: 4 }}>{roleLabel}</Tag> : null}
              </span>
            ) : null}
          </>
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
  if (seg[0] === 'mu' && seg[1] === 'repos' && seg.length >= 4) {
    return { crumb: [['仓库', '/repos'], [`${seg[2]}/${seg[3]}`, `/repos/${seg[2]}/${seg[3]}`]], current: seg[5] ? `PR #${seg[5]}` : 'PR 详情' };
  }
  if (seg[0] === 'runs' && seg.length > 1) {
    return { crumb: [['运行记录', '/runs']], current: '运行详情' };
  }
  const hit = ALL_NAV.find((n) => pathname === n.to
    || (n.to !== '/' && pathname.startsWith(n.to + '/'))
    || (n.end === false && pathname.startsWith(n.to)));
  return { crumb: null, current: hit?.label ?? '审查工作台' };
}

// 高级工具折叠组（侧栏自定义实现）：
// 默认折叠；子路由激活时自动展开。不使用 antd Submenu——rc-menu 的 CSSMotion/
// PopupTrigger 在 react-test-renderer 冒烟环境崩溃（PR-5 既有坑），且折叠子项
// 惰性渲染依赖 portal。真实 button（aria-expanded）+ NavLink（aria-current）。
function AdvancedNavGroup({ pathname }) {
  const activeChild = ADVANCED_NAV.some((n) => pathname === n.to
    || (n.end === false && pathname.startsWith(n.to)));
  const [open, setOpen] = useState(activeChild);
  useEffect(() => {
    if (activeChild) setOpen(true);
  }, [activeChild]);
  return (
    <div className="nav-adv">
      <button type="button" className={`nav-adv-toggle${activeChild ? ' nav-adv-toggle-active' : ''}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}>
        <ExperimentOutlined aria-hidden />
        <span>高级工具</span>
        <span className="nav-adv-caret" aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open ? (
        <ul className="nav-adv-list">
          {ADVANCED_NAV.map((n) => (
            <li key={n.to}>
              <NavLink to={n.to} end={n.end} className="nav-adv-link"
                aria-label={n.label}>
                <n.icon aria-hidden />
                <span>{n.label}</span>
              </NavLink>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// 侧栏能力边界一行（身份/能力统一映射，见 identity.js——与工作区面板/设置/诊断共用口径）。
// provider 只认会话显式标记，不按 multiuser 模式推断；footer 随会话如实变化。
function capabilityFooterLine(config, auth) {
  const identity = deriveIdentitySource({
    session: { user: auth.user, session_source: auth.user?.session_source },
    dataMode: config?.dataMode,
    authed: auth.status === 'authed',
  });
  return capabilityLine(identity);
}

function Shell() {
  const loc = useLocation();
  const config = useAppConfig();
  const auth = useAuth();
  const { source } = useDataSource(config);
  const ctx = topbarCtx(loc.pathname);
  const [navOpen, setNavOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(
    typeof window !== 'undefined' && window.matchMedia('(max-width: 960px)').matches);
  const [isCompact, setIsCompact] = useState(
    typeof window !== 'undefined' && window.matchMedia('(max-width: 768px)').matches);
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 960px)');
    const on = (e) => setIsMobile(e.matches);
    mq.addEventListener('change', on);
    const mqc = window.matchMedia('(max-width: 768px)');
    const onC = (e) => setIsCompact(e.matches);
    mqc.addEventListener('change', onC);
    return () => { mq.removeEventListener('change', on); mqc.removeEventListener('change', onC); };
  }, []);
  useEffect(() => { setNavOpen(false); }, [loc.pathname]);

  // 顶栏账户区组织/角色（MU 模式 + 已登录）：缓存按会话身份键失效，
  // 登出/换号时由 resetMuAccountSummaryCache 清除（见 AuthChip.doLogout）
  const muSummary = useMuAccountSummary(config?.mode === 'multiuser' && auth.status === 'authed', auth.user);

  // 导航按数据源能力呈现：run 级全量历史仅 snapshot 取证视图提供
  const systemNav = SYSTEM_NAV.filter((n) => !(source.kind !== 'snapshot' && n.to === '/runs'));

  const navMenuToItem = (n) => ({ key: n.to, icon: <n.icon />, label: <NavLink to={n.to}>{n.label}</NavLink> });
  const menuItems = [
    ...NAV.map(navMenuToItem),
    { type: 'group', label: '系统管理', children: systemNav.map(navMenuToItem) },
  ];
  const selectedKey = ALL_NAV
    .filter((n) => loc.pathname === n.to || (n.end === false && loc.pathname.startsWith(n.to)))
    .map((n) => n.to);

  const brand = (
    <div className="brand">
      <BrandMark />
      <div className="brand-text">
        <div className="brand-name">MergePilot</div>
        <div className="brand-sub">审查工作台</div>
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
          <nav aria-label="主导航">
            {menu}
            <AdvancedNavGroup pathname={loc.pathname} />
          </nav>
          <div className="sidebar-foot">
            <div className="foot-row" title="本服务仅监听 127.0.0.1 回环地址；不下发任何凭证。写操作仅限审批决策与审查发起类 POST（服务端 RBAC+CSRF）——不写 GitHub、不自动合并。能力口径与「设置 / 数据源」页一致。">
              <Activity size={12} strokeWidth={1.75} aria-hidden />
              {capabilityFooterLine(config, auth)}
            </div>
          </div>
        </Layout.Sider>
      ) : (
        <Drawer
          placement="left" width={232} open={navOpen} onClose={() => setNavOpen(false)}
          title={brand} styles={{ body: { padding: 0, background: '#0b0f19' } }}
          closeIcon={<MenuOutlined aria-label="关闭菜单" />}
        >
          <nav aria-label="主导航">
            {menu}
            <AdvancedNavGroup pathname={loc.pathname} />
          </nav>
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
            <AuthChip compact={isCompact} summary={muSummary} />
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
              <Route path="/mu/repos/:owner/:name/pr/:prNumber" element={<MuPrDetail />} />
              <Route path="/pending" element={<PendingPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/runs" element={<RunsHistoryRoute />} />
              <Route path="/runs/:packId" element={<RunsDetailRoute />} />
              <Route path="/approvals" element={<ApprovalsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/login" element={<Navigate to="/pending" replace />} />
              <Route path="/rag" element={<Navigate to="/knowledge" replace />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/usage" element={<Navigate to="/knowledge" replace />} />
              <Route path="*" element={<Navigate to="/pending" replace />} />
            </Routes>
          </ErrorBoundary>
        </main>
      </Layout>
    </Layout>
  );
}

// /runs 与 /runs/:packId 同门：run 历史/证据包是 snapshot 取证域，
// MU/contract 等实时源下一律挡在门外（证据包不按租户隔离，不能直达）。
function SnapshotGateMessage({ subject, missing }) {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  return (
    <div className="state-box state-warn" role="status">
      {subject}为 snapshot 取证视图——当前数据源（{source.kind}）{missing}。
      PR 维度历史在各 PR 详情的"运行历史"展开中查看。返回<Link to="/repos">仓库工作台</Link>。
    </div>
  );
}

function RunsHistoryRoute() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  if (source.kind !== 'snapshot') {
    return <SnapshotGateMessage subject="运行历史" missing="不提供 run 级全量历史" />;
  }
  return <RunsPage />;
}

function RunsDetailRoute() {
  const config = useAppConfig();
  const { source } = useDataSource(config);
  if (source.kind !== 'snapshot') {
    return <SnapshotGateMessage subject="运行详情" missing="不提供 run 证据包详情" />;
  }
  return <RunDetailPage />;
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
// /login 是明确的登录入口（含 ?invite= 受邀流）：已登录也渲染登录页——否则
// /login 会经 /pending 重定向吞掉 invite 参数，已登录用户无法发起受邀认领。
// 受邀继续 = 以受邀 GitHub 身份重新走 OAuth（后端按 subject/单次/过期校验）。
function Guarded() {
  const auth = useAuth();
  const config = useAppConfig();
  const location = useLocation();
  const isLoginEntry = location.pathname === '/login';
  // 演示放行依据可信运行模式（双源交叉，防 capabilities 缺失时误判为 legacy）：
  //   /api/health 声明 multiuser（或 capabilities.multiuser）→ 守卫层禁用演示；
  //   health 可达但未声明 primary（模式未知）→ 同样不放行——不得假定未知=可演示 legacy；
  //   放行须：非 multiuser 且（primary 已声明非 multiuser 后端 或 后端明示 legacy_login 可用）。
  //   健康检查不可达时 status=unavailable 本就被 DEMO_BLOCKED 拦截——模式未知绝不放行。
  const multiuserMode = config?.mode === 'multiuser' || auth.capabilities?.multiuser === true;
  const modeKnownNonMu = (config?.modeDeclared === true && config?.mode !== 'multiuser')
    || auth.capabilities?.legacy_login === true;
  const demoAdmitted = auth.demo && !DEMO_BLOCKED.has(auth.status) && !multiuserMode && modeKnownNonMu;
  const admitted = auth.status === 'authed' || demoAdmitted;
  if (auth.status === 'checking') {
    return (
      <div className="login-wrap" role="status">
        <div className="login-card"><p className="login-note">正在检查会话…</p></div>
      </div>
    );
  }
  if (!admitted || isLoginEntry) return <LoginPage />;
  return <Shell />;
}
