// frontend/src/App.jsx — layout shell: topbar + routed pages + demo control bar.
import React from 'react';
import { Routes, Route, Link, NavLink } from 'react-router-dom';
import { useDemo } from './store.jsx';
import Overview from './pages/Overview.jsx';
import CasePage from './pages/CasePage.jsx';
import AuditPage from './pages/AuditPage.jsx';
import RagPage from './pages/RagPage.jsx';
import OpsPage from './pages/OpsPage.jsx';
import Pr4Page from './pages/Pr4Page.jsx';
import { ModeBanner, DemoBar, ErrorBox, LoadingBox } from './components/ui.jsx';

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  componentDidCatch(error, info) {
    console.error('[MergePilot UI error]', error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <main className="page" style={{ paddingTop: 40 }}>
          <div className="statebox error" role="alert">
            页面渲染出错
            <span className="mono">{String(this.state.error && this.state.error.message || this.state.error)}</span>
            <pre className="tl-detail" style={{ textAlign: 'left' }}>{String((this.state.error && this.state.error.stack) || '')}</pre>
            <div style={{ marginTop: 10 }}>
              <button className="btn ghost" onClick={() => window.location.reload()}>重载</button>
            </div>
          </div>
          <DemoBar />
        </main>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const { error, modes, mode } = useDemo();
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" style={{ textDecoration: 'none', color: 'inherit' }}>
            <div className="brand">
              <span className="brand-name">Merge<span className="mp">Pilot</span></span>
              <span className="brand-sub">多 Agent 审查 · 受控修复 · 人工安全门</span>
            </div>
          </Link>
          <nav className="topnav">
            <NavLink to="/" end>总览</NavLink>
            <NavLink to="/cases/pr1-normal-review">PR#1</NavLink>
            <NavLink to="/cases/pr2-high-risk-human-gate">PR#2</NavLink>
            <NavLink to="/cases/pr3-high-risk-human-reject">PR#3</NavLink>
            <NavLink to="/pr4">PR#4</NavLink>
            <NavLink to="/rag">RAG</NavLink>
            <NavLink to="/ops">Operations</NavLink>
            <NavLink to="/audit">审计</NavLink>
          </nav>
          <div className="topbar-right">
            <ModeBanner modes={modes} mode={mode} />
          </div>
        </div>
      </header>

      {error && <main className="page" style={{ paddingTop: 30 }}><ErrorBox e={error} /></main>}
      {!error && (
        <ErrorBoundary>
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/cases/:caseId" element={<CasePage />} />
            <Route path="/audit" element={<AuditPage />} />
            <Route path="/pr4" element={<Pr4Page />} />
            <Route path="/rag" element={<RagPage />} />
            <Route path="/ops" element={<OpsPage />} />
            <Route path="*" element={<main className="page"><LoadingBox text="加载中…" /></main>} />
          </Routes>
        </ErrorBoundary>
      )}

      <DemoBar />
      <footer className="pagefoot">
        MergePilot Demo Platform · Phase 14.2H-WD · 人工门/任务状态/审计均来自真实 AgentTeams/CoPaw 证据，前端不伪造
      </footer>
    </>
  );
}
