// frontend/src/DemoChrome.jsx — header/footer for the finals guided-demo routes.
// Legacy replay pages (/cases/pr*, /pr4, /rework, /rag, /audit, /ops) keep the
// original dark chrome in App.jsx; this shell only wraps the light demo world.
import React, { useEffect, useState } from 'react';
import { Link, NavLink } from 'react-router-dom';
import { ChevronDown } from 'lucide-react';
import { api } from './api.js';

const LEGACY = [
  ['/cases/pr1-normal-review', 'PR #1 普通协同', 'HISTORICAL_REPLAY'],
  ['/cases/pr2-high-risk-human-gate', 'PR #2 高危人工门（批准）', 'HISTORICAL_REPLAY'],
  ['/cases/pr3-high-risk-human-reject', 'PR #3 高危人工门（拒绝）', 'HISTORICAL_REPLAY'],
  ['/pr4', 'PR #4 跨仓 Schema（模拟对比）', 'SYNTHETIC / LOCAL_REAL_SQL'],
  ['/rework', '返工闭环全量证据页', 'CONTROL_PLANE_MECHANISM'],
  ['/rag', 'RAG 检索（合成语料）', 'SYNTHETIC'],
  ['/ops', 'Operations', '系统边界'],
  ['/audit', '审计', '完整性与残余风险'],
];

export default function DemoChrome({ children }) {
  const [integ, setInteg] = useState(null);
  useEffect(() => {
    document.body.classList.add('dbody');
    api('/api/demo/overview').then((o) => setInteg(o.platform?.integrity ?? null)).catch(() => setInteg(null));
    return () => document.body.classList.remove('dbody');
  }, []);
  const verified = integ ? integ.filter((d) => d.verified).length : null;
  const total = integ ? integ.length : null;

  return (
    <div className="dpage" style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>
      <header className="dtop">
        <div className="dtop-inner">
          <Link to="/" className="dbrand" aria-label="MergePilot 首页">
            <b>Merge<i>Pilot</i></b>
            <span className="denv">决赛演示 · 离线回放</span>
          </Link>
          <nav className="dnav" aria-label="主导航">
            <NavLink to="/" end className={({ isActive }) => (isActive ? 'on' : '')}>总览</NavLink>
            <NavLink to="/cases" end className={({ isActive }) => (isActive ? 'on' : '')}>案例选择</NavLink>
            <NavLink to="/evidence" className={({ isActive }) => (isActive ? 'on' : '')}>全部证据</NavLink>
            <details className="dmore">
              <summary className="dmore-btn" style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>历史回放包 <ChevronDown size={13} /></summary>
              <div className="dmore-menu">
                {LEGACY.map(([to, label, tier]) => (
                  <Link key={to} to={to}><span>{label}</span><small className="mono">{tier}</small></Link>
                ))}
              </div>
            </details>
          </nav>
          <div className="dtop-right">
            <span className="dinteg" title="决赛证据目录 SHA256SUMS 启动时逐文件重算">
              <span className="dot" style={{ background: verified === total && total ? 'var(--s-green)' : 'var(--s-amber)' }} />
              <span className="txt">证据 SHA256</span>
              <span className="mono tnum">{verified === null ? '…' : `${verified}/${total}`}</span>
            </span>
          </div>
        </div>
      </header>
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
      <footer className="dfoot">
        <div className="dfoot-inner">
          <span>MergePilot 决赛演示 · 证据驱动的 PR 审修流程回放器</span>
          <span>数据来源标注：本次真实运行 · 历史回放 · 控制面机制 · 本地真实 SQL · 合成语料 · 未执行</span>
          <span>不依赖实时模型 / GitHub / Matrix / PolarDB；审批与合并均不写入运行系统</span>
        </div>
      </footer>
    </div>
  );
}
