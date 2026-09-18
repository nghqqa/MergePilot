// frontend/src/pages/demo/CaseSelectorPage.jsx — the full case portfolio.
// Every card is a working navigation entry; each shows risk, purpose,
// execution nature (evidence level) and verdict so 历史/合成/实时执行 are
// never conflated.
import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Play, FileText, ArrowRight, GitPullRequest, Database } from 'lucide-react';
import { api } from '../../api.js';
import { LevelChip, Verdict, LEVEL_ZH } from '../../components/demo/bits.jsx';

const short = (s, n = 12) => (s ? `${String(s).slice(0, n)}…` : '未提供');

export default function CaseSelectorPage() {
  const [o, setO] = useState(null);
  const [err, setErr] = useState(null);
  const nav = useNavigate();
  useEffect(() => { api('/api/demo/cases').then(setO).catch(setErr); }, []);

  if (err) return <main className="dwrap"><div className="dstate error">案例接口不可达：{err.message}</div></main>;
  if (!o) return <main className="dwrap"><div className="dstate"><span className="spin">◌</span> 读取案例…</div></main>;

  const openCase = (c) => nav(c.shape === 'guided' ? `/demo/${c.case_id}?step=1` : `/demo/${c.case_id}`);

  return (
    <main className="dwrap">
      <div className="dhead-row">
        <div className="grow">
          <h1 className="dh1">选择案例</h1>
          <p className="dsub">共 {o.cases.length} 个案例，全部来自锁定证据目录，离线可用。每张卡标注执行性质与证据等级：真实执行 / 历史回放 / 控制面机制 / 本地真实 SQL / 合成语料，互不混写。</p>
        </div>
      </div>

      <div className="legend-strip" aria-label="证据等级图例">
        <span className="small faint">证据等级：</span>
        {Object.entries(LEVEL_ZH).map(([k, zh]) => <LevelChip key={k} level={k} />)}
      </div>

      <div style={{ display: 'grid', gap: 12 }}>
        {o.cases.map((c) => (
          <div
            className="casecard"
            key={c.case_id}
            role="link"
            tabIndex={0}
            style={{ cursor: 'pointer' }}
            onClick={() => openCase(c)}
            onKeyDown={(e) => { if (e.key === 'Enter') openCase(c); }}
          >
            <div className="cc-main">
              <div className="cc-title">
                {c.shape === 'guided' ? <Play size={15} className="faint" /> : <Database size={15} className="faint" />}
                {c.portfolio_role ? <span className="chip slate">{c.portfolio_role}</span> : null}
                {c.name}
              </div>
              <div className="cc-line">{c.one_liner}</div>
              <div className="cc-meta">用途：{c.purpose || '未提供'}</div>
              <div className="cc-meta">{c.repo}{c.pr ? ` · ${c.pr}` : ''} · run {c.run_id} · SHA {short(c.sha, 12)}（{c.sha_kind}）</div>
              <div className="cc-meta">风险标签：{(c.risk_tags || []).join(' · ') || '—'}{c.relation_note ? <span style={{ display: 'block', marginTop: 2 }}>{c.relation_note}</span> : null}</div>
            </div>
            <div className="cc-side">
              <div className="cc-tags">
                <Verdict v={c.status.verdict}>{c.status.verdict}</Verdict>
                <LevelChip level={c.evidence_level} />
              </div>
              <div className="small muted" style={{ textAlign: 'right' }}>{c.status.label}</div>
              <button className="btn primary small" onClick={(e) => { e.stopPropagation(); openCase(c); }}>
                {c.shape === 'guided' ? '开始引导演示' : '查看案例简报'} <ArrowRight size={13} />
              </button>
            </div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 16, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <FileText size={14} className="faint" />
        <span className="small muted">需要完整调度细节（outbox / stage_events / 负向测试）？见</span>
        <a className="evlink" href="#" onClick={(e) => { e.preventDefault(); nav('/evidence'); }}>全部证据 <ArrowRight size={12} /></a>
      </div>
    </main>
  );
}
