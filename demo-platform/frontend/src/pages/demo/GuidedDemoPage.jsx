// frontend/src/pages/demo/GuidedDemoPage.jsx — the live stage. Five fixed nodes
// (PR 发起 → 审查发现 → 修复 diff → 独立验证 → 人工审批·处置), step bar on top,
// prev/next + jump anywhere, current highlighted, completed marked. No autoplay,
// no network beyond the local demo backend. Step index lives in ?step= so a
// presenter can deep-link; demo approval state is React memory and resets on
// refresh (and via the reset button).
import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams, useSearchParams, Link } from 'react-router-dom';
import {
  ChevronLeft, ChevronRight, RotateCcw, ArrowLeft, Eye, GitPullRequest, ScanSearch, FileDiff, FlaskConical, UserCheck,
} from 'lucide-react';
import { api } from '../../api.js';
import { LevelChip, Verdict, Points, Collapsed, StepBar, CodeBlock } from '../../components/demo/bits.jsx';
import EvidenceDrawer from '../../components/demo/EvidenceDrawer.jsx';
import DecisionPanel, { NotExecutedNote } from './DecisionPanel.jsx';

const ICONS = [GitPullRequest, ScanSearch, FileDiff, FlaskConical, UserCheck];
const short = (s, n = 12) => (s ? `${String(s).slice(0, n)}…` : '未提供');

export default function GuidedDemoPage({ caseId: propCaseId }) {
  const params = useParams();
  const caseId = propCaseId || params.caseId;
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
  const [c, setC] = useState(null);
  const [err, setErr] = useState(null);
  const [drawer, setDrawer] = useState(null); // evidence id
  const [resetKey, setResetKey] = useState(0);

  useEffect(() => { let alive = true; api(`/api/demo/cases/${caseId}`).then((r) => alive && setC(r)).catch((e) => alive && setErr(e)); return () => { alive = false; }; }, [caseId]);

  // ?ev=<evidence id> deep link: open the evidence drawer once the case is loaded.
  const evParam = sp.get('ev');
  useEffect(() => { if (evParam && c && (c.items || []).some((x) => x.id === evParam)) setDrawer(evParam); }, [evParam, c]); // eslint-disable-line

  const total = c?.steps?.length ?? 5;
  const stepIdx = Math.min(Math.max((Number(sp.get('step')) || 1) - 1, 0), total - 1);
  const go = (i) => { const n = Math.min(Math.max(i, 0), total - 1); setSp({ step: String(n + 1) }, { replace: false }); };

  // ← / → move between steps when the drawer is closed and no input is focused.
  useEffect(() => {
    const h = (e) => {
      if (drawer || e.target.closest('input,textarea,select,[contenteditable]')) return;
      if (e.key === 'ArrowRight') { e.preventDefault(); go(stepIdx + 1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); go(stepIdx - 1); }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [stepIdx, drawer, total]); // eslint-disable-line

  const itemsById = useMemo(() => Object.fromEntries((c?.items || []).map((x) => [x.id, x])), [c]);

  if (err) return <main className="dwrap"><div className="dstate error">案例接口不可达：{err.message}</div></main>;
  if (!c) return <main className="dwrap"><div className="dstate"><span className="spin">◌</span> 读取案例证据…</div></main>;
  if (c.available === false) return <main className="dwrap"><div className="notice amber">证据未提供：{c.reason}。本页不显示任何结果。</div></main>;

  const step = c.steps[stepIdx];
  const Icon = ICONS[stepIdx] || Eye;
  const reset = () => { setResetKey((k) => k + 1); setDrawer(null); setSp({ step: '1' }); };
  const chain = Array.isArray(c.chain) && c.chain.length > 0
    ? c.chain
    : ['案例 ' + c.case_id, c.pr.split(' · ')[0], `tree ${short(c.sha, 10)}`, `任务 ${c.status.verdict}`];
  const previewItems = step.evidence.map((id) => itemsById[id]).filter(Boolean);
  const previewBlock = previewItems.map((it) => it.blocks?.[0]).find((b) => b && b.text && b.text !== '未提供');

  return (
    <main className="dwrap" key={resetKey}>
      {/* case header */}
      <div className="dhead-row" style={{ alignItems: 'center' }}>
        <Link to="/cases" className="evlink" style={{ paddingLeft: 0 }}><ArrowLeft size={14} /> 返回案例选择</Link>
        <span className="faint">/</span>
        <div className="grow" style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', minWidth: 0 }}>
          <h1 className="dh1" style={{ fontSize: 18, margin: 0 }}>{c.name}</h1>
          <span className="chip slate mono">{c.pr.split(' · ')[0]}</span>
          <span className="chip slate mono">{c.run_id}</span>
          <LevelChip level={c.evidence_level} />
          <Verdict v={c.status.verdict}>{c.status.verdict} {stepIdx + 1}/{total}</Verdict>
        </div>
        <button className="btn ghost small" onClick={reset} data-tip="回到第 1 步并清除演示审批" aria-label="重置演示"><RotateCcw size={14} /> 重置演示</button>
      </div>

      <StepBar steps={c.steps} current={stepIdx} onJump={go} />

      <div className="ggrid">
        {/* left: key points */}
        <section className="panel">
          <div className="panel-head">
            <Icon size={16} className="faint" />
            <h2 className="dh2">第 {stepIdx + 1} 步 · {step.title}</h2>
            <span className="spacer" />
            <span className="small faint">评委要点 {step.points.length} 条</span>
          </div>
          <div className="panel-body">
            <Points rows={step.points} />
            {step.detail && (
              <Collapsed title={step.detail.title}>
                {step.detail.quote && <div className="notice gray" style={{ marginBottom: 10 }}>{step.detail.quote}</div>}
                {step.detail.outbox?.length > 0 && (
                  <div className="table-scroll">
                    <table className="dtable">
                      <thead><tr><th>target</th><th>stage</th><th>attempt</th><th>body</th></tr></thead>
                      <tbody>
                        {step.detail.outbox.map((o, i) => (
                          <tr key={i}><td className="mono">{o.target_agent}</td><td className="mono">{o.target_stage}</td><td className="mono tnum">{o.attempt}</td><td className="wrap small">{o.body}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {step.detail.events?.length > 0 && (
                  <div className="table-scroll" style={{ marginTop: 8 }}>
                    <table className="dtable">
                      <thead><tr><th>event_id</th><th>sender</th><th>type</th><th>stage</th><th>status</th></tr></thead>
                      <tbody>
                        {step.detail.events.map((e) => (
                          <tr key={e.event_id}><td className="mono wrap">{e.event_id}</td><td className="mono wrap small">{e.sender}</td><td className="mono">{e.event_type}</td><td className="mono">{e.stage}</td><td className="mono">{e.status}</td></tr>
                        ))}
                      </tbody>
                    </table>
                    <div className="small faint" style={{ marginTop: 6 }}>event_id 记录于真实 PostgreSQL 审计链；Matrix/Element 真实投递在本证据等级为 NOT_EXECUTED。</div>
                  </div>
                )}
              </Collapsed>
            )}
          </div>
        </section>

        {/* right: evidence rail */}
        <aside className="panel">
          <div className="panel-head">
            <Eye size={16} className="faint" />
            <h2 className="dh2">本步证据</h2>
            <span className="spacer" />
            <span className="small faint">{previewItems.length} 条</span>
          </div>
          <div className="panel-body" style={{ paddingTop: 6 }}>
            {previewItems.map((it) => (
              <div className="evrow" key={it.id}>
                <div className="t">{it.title}<div style={{ marginTop: 3 }}><LevelChip level={it.level} zh={false} /></div></div>
                <button className="evlink" onClick={() => setDrawer(it.id)}>查看证据 <ChevronRight size={13} /></button>
              </div>
            ))}
            {previewBlock && (
              <div style={{ marginTop: 12 }}>
                <CodeBlock title={previewBlock.title} text={previewBlock.text} lang={previewBlock.lang} max={260} />
              </div>
            )}
          </div>
        </aside>
      </div>

      {stepIdx === total - 1 && (
        <>
          <DecisionPanel decision={step.decision} chain={chain} />
          <NotExecutedNote items={c.honesty?.not_executed} />
        </>
      )}

      {/* bottom navigation */}
      <div className="gnav" role="navigation" aria-label="步骤导航">
        <button className="btn ghost small" onClick={() => go(stepIdx - 1)} disabled={stepIdx === 0}><ChevronLeft size={14} /> 上一步</button>
        <button className="btn primary small" onClick={() => go(stepIdx + 1)} disabled={stepIdx >= total - 1}>下一步 <ChevronRight size={14} /></button>
        <span className="pos"><b className="tnum">步骤 {stepIdx + 1} / {total}</b><span>{step.title}</span></span>
        <span className="right">
          <span className="kbd"><kbd>←</kbd> <kbd>→</kbd> 切换步骤 · <kbd>Esc</kbd> 关闭证据</span>
          <Link className="evlink" to="/cases">返回案例选择</Link>
        </span>
      </div>

      <EvidenceDrawer caseId={c.case_id} evidenceId={drawer} onClose={() => setDrawer(null)} />
    </main>
  );
}
