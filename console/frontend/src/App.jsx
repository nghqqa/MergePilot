import React, { useEffect, useState } from 'react';
import { NavLink, Route, Routes, useLocation } from 'react-router-dom';
import {
  Activity, Database, FolderGit2, Gauge, ListChecks, ShieldCheck, Wrench,
} from 'lucide-react';
import { api } from './api.js';
import { fmtTime } from './format.js';
import RunsPage from './pages/RunsPage.jsx';
import RunDetailPage from './pages/RunDetailPage.jsx';
import StubPage from './pages/StubPage.jsx';

// 品牌标：两条分支汇入一条主干（merge），自绘 SVG，深青单色。
function BrandMark() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d="M6 4v6c0 3 2 5 5 5h7" stroke="#2dd4bf" strokeWidth="2" strokeLinecap="round" />
      <path d="M18 4v6c0 1.5-.6 2.8-1.6 3.8" stroke="#0e6b62" strokeWidth="2" strokeLinecap="round" opacity="0.85" />
      <circle cx="6" cy="4" r="2.2" fill="#2dd4bf" />
      <circle cx="18" cy="4" r="2.2" fill="#0e6b62" />
      <circle cx="18" cy="15" r="2.2" fill="#e6f2f0" />
    </svg>
  );
}

function TopbarContext() {
  // 数据模式 + 采集范围：静态事实标注，不用跳动时钟暗示实时。
  const [range, setRange] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    api.health().then(setErr(null)).catch(setErr);
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
      <span
        className={`mode-chip ${err ? 'mode-chip-bad' : ''}`}
        title={
          err
            ? `后端不可达：${err.message}`
            : '数据模式 snapshot：全部数据来自仓库内锁定的真实历史运行证据包（SHA256SUMS 校验、只读）。live 实时模式未接入；服务仅监听 127.0.0.1 回环地址，无写操作接口，不下发凭证。'
        }
      >
        <span className={`mode-dot ${err ? 'mode-dot-bad' : ''}`} aria-hidden />
        <strong>{err ? '后端不可达' : '历史快照 · 只读'}</strong>
      </span>
      {!err && range ? <span className="mode-range">数据采集于 {range}</span> : null}
    </span>
  );
}

const NAV_SECTIONS = [
  {
    label: '审查',
    items: [
      { to: '/runs', label: '审查运行', icon: Activity, ready: true, end: true },
    ],
  },
  {
    label: '尚未接入',
    items: [
      { to: '/repos', label: '仓库', icon: FolderGit2, ready: false, note: '仓库接入状态依赖 GitHub App installation 数据源（未接入）' },
      { to: '/rag', label: 'RAG', icon: Database, ready: false, note: 'RAG 服务状态依赖 rag-live :4184 与检索调用记录（run 详情内已有历史调用快照）' },
      { to: '/skills', label: 'Skill', icon: Wrench, ready: false, note: 'Skill 版本总览依赖 MinIO skill store / worker 上报（未接入；run 详情内有包内记录）' },
      { to: '/approvals', label: '审批', icon: ShieldCheck, ready: false, note: '审批需 M2 决策项 D-1/D-2/D-3 拍板 + 票据存储落地（后端会话负责）' },
      { to: '/usage', label: '用量', icon: Gauge, ready: false, note: '用量总览依赖 usage 数据源接入（run 详情内已有包内记录）' },
    ],
  },
];

export default function App() {
  const loc = useLocation();
  useEffect(() => {
    document.title = 'MergePilot Console';
  }, []);

  const sectionByPath = () => {
    if (loc.pathname.startsWith('/runs/')) return { crumb: ['审查运行', '/runs'], current: '运行详情' };
    const flat = NAV_SECTIONS.flatMap((s) => s.items);
    const hit = flat.find((n) => loc.pathname.startsWith(n.to));
    return { crumb: null, current: hit?.label ?? '控制台' };
  };
  const ctx = sectionByPath();

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
          {NAV_SECTIONS.map((sec) => (
            <div key={sec.label} className="nav-section">
              <div className="nav-section-label">{sec.label}</div>
              {sec.items.map((n) => (
                <NavLink
                  key={n.to}
                  to={n.ready ? n.to : `${n.to}#stub`}
                  end={n.end}
                  className={({ isActive }) => `nav-item${isActive ? ' active' : ''}${n.ready ? '' : ' nav-stub'}`}
                  title={n.ready ? undefined : `${n.label}：未接入 — ${n.note}`}
                  onClick={(e) => {
                    if (!n.ready) e.preventDefault();
                  }}
                >
                  <n.icon size={15} strokeWidth={1.75} aria-hidden />
                  <span className="nav-label">{n.label}</span>
                  {!n.ready && <span className="nav-tag">未接入</span>}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="foot-row" title="本服务仅监听 127.0.0.1 回环地址；不下发任何凭证">
            <ListChecks size={12} strokeWidth={1.75} aria-hidden /> 只读 · 无写操作 · 无凭证下发
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar-context">
            {ctx.crumb ? (
              <span className="crumb">
                <NavLink to={ctx.crumb[1]}>{ctx.crumb[0]}</NavLink>
                <span className="crumb-sep">/</span>
                <span className="crumb-current">{ctx.current}</span>
              </span>
            ) : (
              <span className="crumb-current">{ctx.current}</span>
            )}
          </div>
          <div className="topbar-right">
            <TopbarContext />
          </div>
        </header>
        <main className="content" key={loc.pathname}>
          <Routes>
            <Route path="/" element={<RunsPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:packId" element={<RunDetailPage />} />
            <Route path="/repos" element={<StubPage section="仓库" icon={FolderGit2} />} />
            <Route path="/rag" element={<StubPage section="RAG" icon={Database} />} />
            <Route path="/skills" element={<StubPage section="Skill" icon={Wrench} />} />
            <Route path="/approvals" element={<StubPage section="审批" icon={ShieldCheck} />} />
            <Route path="/usage" element={<StubPage section="用量" icon={Gauge} />} />
            <Route path="*" element={<StubPage section="页面" icon={Activity} />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
