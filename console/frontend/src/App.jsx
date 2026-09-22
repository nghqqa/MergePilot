import React, { useEffect, useState } from 'react';
import { NavLink, Route, Routes, useLocation } from 'react-router-dom';
import { api } from './api.js';
import RunsPage from './pages/RunsPage.jsx';
import RunDetailPage from './pages/RunDetailPage.jsx';
import StubPage from './pages/StubPage.jsx';
import { fmtTime } from './format.js';

function DataModeBanner() {
  const [health, setHealth] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    api.health().then(setHealth).catch(setErr);
  }, []);
  return (
    <div className="mode-banner" title="控制台当前数据模式说明">
      <span className={`dot ${err ? 'dot-bad' : 'dot-info'}`} aria-hidden />
      {err ? (
        <>后端不可达 — {err.message}</>
      ) : health ? (
        <>
          数据模式 <strong>snapshot</strong>（真实历史证据包 · 只读 · 非实时）· {health.runs} 个运行包 ·
          快照范围 {health.packs_with_sums}/{health.runs} 带 SHA256SUMS · live 未接入
        </>
      ) : (
        '连接后端中…'
      )}
    </div>
  );
}

const NAV = [
  { to: '/runs', label: '运行', ready: true, end: false },
  { to: '/repos', label: '仓库', ready: false, note: '仓库接入状态依赖 GitHub App installation 数据源（未接入）' },
  { to: '/rag', label: 'RAG', ready: false, note: 'RAG 服务状态依赖 rag-live :4184 与检索调用记录（run 详情内已有历史调用快照）' },
  { to: '/skills', label: 'Skill', ready: false, note: 'Skill 版本总览依赖 MinIO skill store / worker 上报（未接入；run 详情内有包内记录）' },
  { to: '/approvals', label: '审批', ready: false, note: '审批需 M2 决策项 D-1/D-2/D-3 拍板 + 票据存储落地（后端会话负责）' },
  { to: '/usage', label: '用量', ready: false, note: '用量总览依赖 usage 数据源接入（run 详情内已有包内记录）' },
];

export default function App() {
  const loc = useLocation();
  useEffect(() => {
    document.title = 'MergePilot Console';
  }, []);
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-name">MergePilot</div>
          <div className="brand-sub">管理控制台 V0</div>
        </div>
        <nav aria-label="主导航">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.ready ? n.to : `${n.to}#stub`}
              className={({ isActive }) => `nav-item${isActive ? ' active' : ''}${n.ready ? '' : ' nav-stub'}`}
              title={n.ready ? undefined : `${n.label}：未接入 — ${n.note}`}
              onClick={(e) => {
                if (!n.ready) e.preventDefault();
              }}
            >
              {n.label}
              {!n.ready && <span className="nav-tag">未接入</span>}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>仅监听 127.0.0.1</div>
          <div>无写操作 · 无凭证下发</div>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <DataModeBanner />
          <span className="topbar-time">{fmtTime(new Date().toISOString())} 本地</span>
        </header>
        <main className="content" key={loc.pathname}>
          <Routes>
            <Route path="/" element={<RunsPage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/runs/:packId" element={<RunDetailPage />} />
            <Route path="/repos" element={<StubPage section="仓库" />} />
            <Route path="/rag" element={<StubPage section="RAG" />} />
            <Route path="/skills" element={<StubPage section="Skill" />} />
            <Route path="/approvals" element={<StubPage section="审批" />} />
            <Route path="/usage" element={<StubPage section="用量" />} />
            <Route path="*" element={<StubPage section="页面" />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
