// frontend/src/pages/demo/CaseBriefPage.jsx — the short case. Exactly four
// blocks (风险类型 → 关键证据 → 验证结论 → 最终处置) plus the decision strip.
// No step navigation, no extra capability claims.
import React, { useEffect, useState } from 'react';
import { useParams, useSearchParams, Link } from 'react-router-dom';
import { ArrowLeft, Database, ChevronRight, RotateCcw } from 'lucide-react';
import { api } from '../../api.js';
import { LevelChip, Verdict, Points, EvidenceLinkRow } from '../../components/demo/bits.jsx';
import EvidenceDrawer from '../../components/demo/EvidenceDrawer.jsx';
import DecisionPanel, { NotExecutedNote } from './DecisionPanel.jsx';

const short = (s, n = 12) => (s ? `${String(s).slice(0, n)}…` : '未提供');

export default function CaseBriefPage({ caseId: propCaseId }) {
  const params = useParams();
  const caseId = propCaseId || params.caseId;
  const [sp, setSp] = useSearchParams();
  const [c, setC] = useState(null);
  const [err, setErr] = useState(null);
  const [drawer, setDrawer] = useState(null);
  const [resetKey, setResetKey] = useState(0);

  useEffect(() => { let alive = true; api(`/api/demo/cases/${caseId}`).then((r) => alive && setC(r)).catch((e) => alive && setErr(e)); return () => { alive = false; }; }, [caseId]);

  if (err) return <main className="dwrap"><div className="dstate error">案例接口不可达：{err.message}</div></main>;
  if (!c) return <main className="dwrap"><div className="dstate"><span className="spin">◌</span> 读取案例证据…</div></main>;
  if (c.available === false) return <main className="dwrap"><div className="notice amber">证据未提供：{c.reason}。本页不显示任何结果。</div></main>;

  return (
    <main className="dwrap" key={resetKey}>
      <div className="dhead-row" style={{ alignItems: 'center' }}>
        <Link to="/cases" className="evlink" style={{ paddingLeft: 0 }}><ArrowLeft size={14} /> 返回案例总览</Link>
        <span className="faint">/</span>
        <div className="grow" style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', minWidth: 0 }}>
          <h1 className="dh1" style={{ fontSize: 18, margin: 0 }}>{c.name}</h1>
          <span className="chip slate mono">{c.pr.split(' · ')[0]}</span>
          <span className="chip slate mono">{c.run_id}</span>
          <LevelChip level={c.evidence_level} />
          <Verdict v={c.status.verdict}>{c.status.verdict} 11/11</Verdict>
        </div>
        <button className="btn ghost small" onClick={() => { setResetKey((k) => k + 1); setDrawer(null); setSp({}); }} data-tip="清除抽屉与滚动状态" aria-label="重置演示"><RotateCcw size={14} /> 重置演示</button>
      </div>
      <p className="dsub" style={{ marginBottom: 4 }}>{c.one_liner}</p>

      <div className="ov-meta" style={{ marginTop: 10 }}>
        <div><span className="k">代码 SHA</span><span className="v" title={c.sha || ''}>{short(c.sha, 14)} <span className="faint" style={{ fontFamily: 'inherit', fontSize: 11 }}>({c.sha_kind})</span></span></div>
        <div><span className="k">证据生成</span><span className="v">{c.generated_at ?? '未提供'}</span></div>
        <div><span className="k">证据目录</span><span className="v">{c.source_dir}</span></div>
      </div>

      {c.blocks.map((b, i) => (
        <section className="panel brief-block" key={b.id}>
          <div className="panel-head">
            <span className="brief-num">{i + 1}</span>
            <h2 className="dh2">{b.title}</h2>
            <span className="spacer" />
            <span className="small faint">证据 {b.evidence.length} 条</span>
          </div>
          <div className="panel-body" style={{ display: 'grid', gap: 0 }}>
            <Points rows={b.rows.map((r) => ({ k: r.k, v: r.v, mono: r.mono }))} />
            <EvidenceLinkRow items={b.evidence} itemsById={Object.fromEntries((c.items || []).map((x) => [x.id, x]))} onOpen={setDrawer} />
          </div>
        </section>
      ))}

      <DecisionPanel decision={c.decision} chain={['案例 ' + c.case_id, c.pr.split(' · ')[0], `head ${short(c.sha, 10)}`, 'migration SQL', '11/11 断言', '审批绑定 → follow-up 后 STALE']} />
      <NotExecutedNote items={c.honesty?.not_executed} />

      <div className="gnav" role="navigation" aria-label="页面导航">
        <span className="pos"><Database size={14} /> <b>次案例</b><span>短简报 · 完整负向测试与 gate 时间线见全部证据</span></span>
        <span className="right">
          <span className="kbd"><kbd>Esc</kbd> 关闭证据</span>
          <Link className="evlink" to="/evidence">全部证据 <ChevronRight size={12} /></Link>
        </span>
      </div>

      <EvidenceDrawer caseId={c.case_id} evidenceId={drawer} onClose={() => setDrawer(null)} />
    </main>
  );
}
