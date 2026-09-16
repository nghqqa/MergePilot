// frontend/src/pages/demo/CaseSelectorPage.jsx — exactly two cases. Each row
// shows risk tags, verification status and evidence level; the six fixed level
// labels are spelled out so 历史/合成/实时执行 are never conflated.
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

  return (
    <main className="dwrap">
      <div className="eyebrow"><GitPullRequest size={13} /> Case Selector · 案例选择</div>
      <div className="dhead-row">
        <div className="grow">
          <h1 className="dh1">两个演示案例</h1>
          <p className="dsub">全部为锁定证据目录的历史回放，离线可用；每个案例的执行性质按证据等级明确区分，不混写历史回放、合成数据与实时执行。</p>
        </div>
      </div>

      <div className="legend-strip" aria-label="证据等级图例">
        <span className="small faint">证据等级：</span>
        {Object.entries(LEVEL_ZH).map(([k, zh]) => <LevelChip key={k} level={k} />)}
      </div>

      <div style={{ display: 'grid', gap: 12 }}>
        {o.cases.map((c) => (
          <div className="casecard" key={c.case_id}>
            <div className="cc-main">
              <div className="cc-title">
                {c.shape === 'guided' ? <Play size={15} className="faint" /> : <Database size={15} className="faint" />}
                {c.name}
                <span className="chip slate">{c.shape === 'guided' ? '主案例 · 完整闭环' : '次案例 · 横向证明'}</span>
              </div>
              <div className="cc-line">{c.one_liner}</div>
              <div className="cc-meta">{c.repo} · {c.pr} · run {c.run_id} · SHA {short(c.sha, 12)}（{c.sha_kind}）</div>
              <div className="cc-meta">风险标签：{c.risk_tags.join(' · ')}</div>
            </div>
            <div className="cc-side">
              <div className="cc-tags">
                <Verdict v={c.status.verdict}>{c.status.verdict}</Verdict>
                <LevelChip level={c.evidence_level} />
              </div>
              <div className="small muted" style={{ textAlign: 'right' }}>{c.status.label}</div>
              <button className="btn primary small" onClick={() => nav(c.shape === 'guided' ? `/demo/${c.case_id}?step=1` : `/demo/${c.case_id}`)}>
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
