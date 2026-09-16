// frontend/src/App.jsx — dual-chrome shell.
//   Demo world (light, finals guided replay): / , /cases , /demo/:caseId , /evidence
//     → DemoChrome (new header/footer, no autoplay controls).
//   Legacy replay world (dark, Phase 14): /cases/pr* , /pr4 , /rework , /rag ,
//   /audit , /ops → original topbar + DemoBar, untouched.
import React from 'react';
import { Routes, Route, useLocation } from 'react-router-dom';
import { useDemo } from './store.jsx';
import DemoChrome from './DemoChrome.jsx';
import OverviewPage from './pages/demo/OverviewPage.jsx';
import CaseSelectorPage from './pages/demo/CaseSelectorPage.jsx';
import GuidedDemoPage from './pages/demo/GuidedDemoPage.jsx';
import CaseBriefPage from './pages/demo/CaseBriefPage.jsx';
import EvidenceLibraryPage from './pages/demo/EvidenceLibraryPage.jsx';
import CasePage from './pages/CasePage.jsx';
import AuditPage from './pages/AuditPage.jsx';
import RagPage from './pages/RagPage.jsx';
import OpsPage from './pages/OpsPage.jsx';
import Pr4Page from './pages/Pr4Page.jsx';
import ReworkPage from './pages/ReworkPage.jsx';
import Overview from './pages/Overview.jsx';
import { DemoBar, ErrorBox, LoadingBox } from './components/ui.jsx';

function DemoRoutes() {
  return (
    <Routes>
      <Route path="/" element={<OverviewPage />} />
      <Route path="/cases" element={<CaseSelectorPage />} />
      {/* shape-specific demo pages; unknown ids fall through to the selector */}
      <Route path="/demo/fastapi-pr2-r2-live-20260916" element={<GuidedDemoPage caseId="fastapi-pr2-r2-live-20260916" />} />
      <Route path="/demo/fastapi-pr2-live-20260916" element={<GuidedDemoPage caseId="fastapi-pr2-live-20260916" />} />
      <Route path="/demo/fastapi-pr2-cwe22" element={<GuidedDemoPage caseId="fastapi-pr2-cwe22" />} />
      <Route path="/demo/fastapi-pr3-reject" element={<GuidedDemoPage caseId="fastapi-pr3-reject" />} />
      <Route path="/demo/rag-retrieval-loop" element={<GuidedDemoPage caseId="rag-retrieval-loop" />} />
      <Route path="/demo/rework-payments" element={<GuidedDemoPage caseId="rework-payments" />} />
      <Route path="/demo/db-migration-orders" element={<CaseBriefPage caseId="db-migration-orders" />} />
      <Route path="/demo/:caseId" element={<CaseSelectorPage />} />
      <Route path="/evidence" element={<EvidenceLibraryPage />} />
      <Route path="*" element={<CaseSelectorPage />} />
    </Routes>
  );
}

function LegacyRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Overview />} />
      <Route path="/cases/:caseId" element={<CasePage />} />
      <Route path="/audit" element={<AuditPage />} />
      <Route path="/pr4" element={<Pr4Page />} />
      <Route path="/rework" element={<ReworkPage />} />
      <Route path="/rag" element={<RagPage />} />
      <Route path="/ops" element={<OpsPage />} />
      <Route path="*" element={<main className="page"><LoadingBox text="加载中…" /></main>} />
    </Routes>
  );
}

const isDemoPath = (p) => p === '/' || p === '/cases' || p.startsWith('/demo/') || p === '/evidence';

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
        </main>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const { error } = useDemo();
  const { pathname } = useLocation();
  const demo = isDemoPath(pathname);

  if (demo) {
    return (
      <DemoChrome>
        {error
          ? <main className="dwrap"><div className="dstate error"><ErrorBox e={error} /></div></main>
          : <ErrorBoundary><DemoRoutes /></ErrorBoundary>}
      </DemoChrome>
    );
  }

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <a href="/" style={{ textDecoration: 'none', color: 'inherit' }}>
            <div className="brand">
              <span className="brand-name">Merge<span className="mp">Pilot</span></span>
              <span className="brand-sub">历史回放包（Phase 14）· 旧版视图</span>
            </div>
          </a>
          <nav className="topnav">
            <a href="/cases/pr1-normal-review">PR#1</a>
            <a href="/cases/pr2-high-risk-human-gate">PR#2</a>
            <a href="/cases/pr3-high-risk-human-reject">PR#3</a>
            <a href="/pr4">PR#4</a>
            <a href="/rework">返工闭环</a>
            <a href="/rag">RAG</a>
            <a href="/ops">Operations</a>
            <a href="/audit">审计</a>
            <a href="/">← 决赛演示</a>
          </nav>
        </div>
      </header>

      {error && <main className="page" style={{ paddingTop: 30 }}><ErrorBox e={error} /></main>}
      {!error && (
        <ErrorBoundary>
          <LegacyRoutes />
        </ErrorBoundary>
      )}

      <DemoBar />
      <footer className="pagefoot">
        MergePilot Demo Platform · 历史回放视图 — 人工门/任务状态/审计均来自真实证据，前端不伪造
      </footer>
    </>
  );
}
