// frontend/src/pages/demo/OverviewPage.jsx — demo home. First viewport answers:
// what MergePilot proves, which case is up, whether it is real / replay /
// mechanism, and one primary action (start the main case). Then the full case
// portfolio. No marketing, no decorative cards.
import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Play, FolderSearch, ArrowRight, Database, GitPullRequest, Repeat } from 'lucide-react';
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
  const others = o.cases.filter((c) => c.case_id !== main.case_id);
  const openCase = (c) => nav(c.shape === 'guided' ? `/demo/${c.case_id}?step=1` : `/demo/${c.case_id}`);
  const allOk = o.platform.integrity.every((d) => d.verified);

  return (
    <main className="dwrap">
      <div className="dhead-row">
        <div className="grow">
          <h1 className="dh1">MergePilot · PR 审修闭环演示</h1>
          <p className="dsub">让代码审查结论经过独立验证后，才进入人工合并决策。本页回放锁定证据目录中的真实运行；不连接实时模型 / GitHub / Matrix。</p>
        </div>
      </div>

      {/* main case hero */}
      <div className="casecard">
        <div className="cc-main">
          <div className="cc-title"><GitPullRequest size={15} className="faint" /> {main.name}
            <span className="chip slate">{main.status.label}</span>
          </div>
          <div className="cc-line">{main.one_liner}</div>
          <div className="cc-meta">
            {main.repo} · {main.pr} · SHA {short(main.sha, 14)}（{main.sha_kind}）
          </div>
          <div className="cc-meta">run {main.run_id} · 风险：{(main.risk_tags || []).join(' · ')}</div>
          <div style={{ marginTop: 8 }}><LevelChip level={main.evidence_level} /></div>
        </div>
        <div className="cc-side">
          <div className="cc-tags"><Verdict v={main.status.verdict}>{main.status.verdict}</Verdict></div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'stretch' }}>
            <button className="btn primary" onClick={() => nav(`/demo/${main.case_id}?step=1`)}><Play size={15} /> 开始主案例</button>
            <Link className="btn ghost" to="/evidence"><FolderSearch size={15} /> 查看证据链</Link>
            <Link className="btn ghost" to="/cases"><Repeat size={15} /> 切换案例</Link>
          </div>
        </div>
      </div>

      {/* full portfolio */}
      <section style={{ marginTop: 28 }}>
        <div className="dhead-row" style={{ marginBottom: 10 }}>
          <h2 className="dh2" style={{ margin: 0 }}>全部案例</h2>
          <span className="spacer" />
          <span className="small faint">共 {o.cases.length} 个 · 点击卡片进入</span>
        </div>
        <div style={{ display: 'grid', gap: 12 }}>
          {others.map((c) => (
            <div
              key={c.case_id}
              className="casecard"
              role="link"
              tabIndex={0}
              style={{ cursor: 'pointer' }}
              onClick={() => openCase(c)}
              onKeyDown={(e) => { if (e.key === 'Enter') openCase(c); }}
            >
              <div className="cc-main">
                <div className="cc-title">
                  {c.case_id === 'fastapi-pr2-cwe22'
                    ? <GitPullRequest size={15} className="faint" />
                    : <Database size={15} className="faint" />}
                  {c.portfolio_role ? <span className="chip slate">{c.portfolio_role}</span> : null}
                  {c.name}
                </div>
                <div className="cc-line">{c.one_liner}</div>
                {c.relation_note ? <div className="cc-meta">{c.relation_note}</div> : null}
                <div className="cc-meta">用途：{c.purpose || '未提供'}</div>
                <div className="cc-meta">
                  {c.repo}{c.pr ? ` · ${c.pr}` : ''} · 风险：{(c.risk_tags || []).join(' · ') || '—'}
                </div>
                <div style={{ marginTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
                  <LevelChip level={c.evidence_level} />
                  {c.replay_note ? <span className="small faint">{c.replay_note}</span> : null}
                </div>
              </div>
              <div className="cc-side">
                <div className="cc-tags"><Verdict v={c.status.verdict}>{c.status.verdict}</Verdict></div>
                <button className="btn ghost small" onClick={(e) => { e.stopPropagation(); openCase(c); }}>
                  {c.shape === 'guided' ? '引导演示' : '案例简报'} <ArrowRight size={13} />
                </button>
              </div>
            </div>
          ))}
        </div>
      </section>

      <div className="src-line" style={{ marginTop: 18 }}>
        证据完整性（启动时逐文件重算 SHA256）：{o.platform.integrity.map((d) => `${d.dir} ${d.verified ? '✓' : '✗'} (${d.files})`).join(' · ')}
        {allOk ? ' — 全部通过' : ' — 存在未通过项'}
      </div>
      <div className="src-line">{o.platform.data_note}</div>
    </main>
  );
}
