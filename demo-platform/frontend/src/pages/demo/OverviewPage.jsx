// frontend/src/pages/demo/OverviewPage.jsx — demo home. First viewport answers:
// what is this, which case is up, its status / PR / SHA / evidence level, and
// two entries (start the guided demo, open all evidence). No marketing.
import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Play, FolderSearch, ArrowRight, Database, GitPullRequest } from 'lucide-react';
import { api } from '../../api.js';
import { LevelChip, Verdict } from '../../components/demo/bits.jsx';

const short = (s, n = 12) => (s ? `${String(s).slice(0, n)}…` : '未提供');

export default function OverviewPage() {
  const [o, setO] = useState(null);
  const [err, setErr] = useState(null);
  const nav = useNavigate();
  useEffect(() => { api('/api/demo/overview').then(setO).catch(setErr); }, []);

  if (err) return <main className="dwrap"><div className="dstate error">总览接口不可达：{err.message}</div></main>;
  if (!o) return <main className="dwrap"><div className="dstate"><span className="spin">◌</span> 读取证据目录…</div></main>;
  if (!o.available) return <main className="dwrap"><div className="notice amber">决赛证据目录不完整（FINALS-REWORK-LOOP / FINALS-DB-MIGRATION-LOOP），本页不显示任何结果。</div></main>;

  const main = o.cases.find((c) => c.case_id === o.current_case_id) || o.cases[0];
  const second = o.cases.find((c) => c.case_id !== main.case_id);

  return (
    <main className="dwrap">
      <div className="eyebrow"><GitPullRequest size={13} /> MergePilot · PR 审修闭环演示 · 当前案例</div>
      <div className="dhead-row">
        <div className="grow">
          <h1 className="dh1">{main.name}</h1>
          <p className="dsub">{main.one_liner}</p>
        </div>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', paddingTop: 6 }}>
          <Verdict v={main.status.verdict}>{main.status.verdict}</Verdict>
          <LevelChip level={main.evidence_level} />
        </div>
      </div>

      <div className="ov-meta">
        <div><span className="k">案例状态</span><span className="v" style={{ fontFamily: 'inherit' }}>{main.status.label}</span></div>
        <div><span className="k">仓库 / PR</span><span className="v">{main.repo} · {main.pr}</span></div>
        <div><span className="k">代码 SHA</span><span className="v" title={main.sha || ''}>{short(main.sha, 14)} <span className="faint" style={{ fontFamily: 'inherit', fontSize: 11 }}>({main.sha_kind})</span></span></div>
        <div><span className="k">run_id</span><span className="v">{main.run_id}</span></div>
        <div><span className="k">证据生成</span><span className="v">{main.generated_at ?? '未提供'}</span></div>
      </div>

      <div className="ov-actions">
        <button className="btn primary" onClick={() => nav(`/demo/${main.case_id}?step=1`)}><Play size={15} /> 开始演示</button>
        <Link className="btn ghost" to="/evidence"><FolderSearch size={15} /> 查看全部证据</Link>
        <Link className="btn ghost" to="/cases">案例选择 <ArrowRight size={14} /></Link>
        <span className="small faint" style={{ marginLeft: 4 }}>五步引导 · 不自动播放 · 全部离线</span>
      </div>

      <div className="ov-secondary">
        <div className="eyebrow">横向能力证明 · 次案例</div>
        {second && (
          <div className="casecard">
            <div className="cc-main">
              <div className="cc-title"><Database size={15} className="faint" /> {second.name}</div>
              <div className="cc-line">{second.one_liner}</div>
              <div className="cc-meta">{second.repo} · {second.pr} · SHA {short(second.sha, 12)}</div>
            </div>
            <div className="cc-side">
              <div className="cc-tags"><Verdict v={second.status.verdict}>{second.status.verdict}</Verdict><LevelChip level={second.evidence_level} /></div>
              <button className="btn ghost small" onClick={() => nav(`/demo/${second.case_id}`)}>查看案例简报 <ArrowRight size={13} /></button>
            </div>
          </div>
        )}
        <div className="src-line">
          证据完整性：{o.platform.integrity.map((d) => `${d.dir} ${d.verified ? '✓' : '✗'} (${d.files})`).join(' · ')}
        </div>
        <div className="src-line">{o.platform.data_note}</div>
      </div>
    </main>
  );
}
