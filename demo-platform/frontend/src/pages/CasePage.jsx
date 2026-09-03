// frontend/src/pages/CasePage.jsx — view switcher: Operations | Presentation | Evidence.
// Presentation = Split Theater (user-locked world). Operations = 研发控制台 (default).
// Evidence = 字段级来源 + 证据抽屉。
import React, { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '../api.js';
import { useDemo } from '../store.jsx';
import { LoadingBox, ErrorBox, SourceRef, ModeBanner } from '../components/ui.jsx';
import { SourceBadge, PrecisionBadge } from '../components/EvidenceDrawer.jsx';
import OperationsView from '../components/OperationsView.jsx';
import EvidenceView from '../components/EvidenceView.jsx';
import Stage from '../components/Stage.jsx';
import Filmstrip from '../components/Filmstrip.jsx';
import GatePanel from '../components/GatePanel.jsx';
import ProbeCompare from '../components/ProbeCompare.jsx';
import { IconBranch, IconPr, IconBox, IconUsers, IconGear, IconGate, IconDoc, IconStage, IconCheck } from '../icons.jsx';

const VIEW_LABEL = { operations: 'OPERATIONS', presentation: 'PRESENTATION', evidence: 'EVIDENCE' };

export default function CasePage() {
  const { caseId } = useParams();
  const { idx, setActiveCase, view, mode, modes, playing, setPlaying } = useDemo();
  const [data, setData] = useState(null);
  const [audit, setAudit] = useState(null);
  const [err, setErr] = useState(null);
  const [drawer, setDrawer] = useState(null); // {kind, seq?, task?}

  // deep-linkable drawer: ?drawer=task:fix-1 | event:5 | approval | trace | finding
  useEffect(() => {
    const d = new URLSearchParams(window.location.search).get('drawer');
    if (!d) return;
    const [kind, arg] = d.split(':');
    if (['event', 'task', 'approval', 'trace', 'finding'].includes(kind)) {
      setDrawer(arg && kind === 'event' ? { kind, seq: Number(arg) } : arg ? { kind, task: arg } : { kind });
    }
  }, []);

  useEffect(() => { setActiveCase(caseId); }, [caseId, setActiveCase]);

  useEffect(() => {
    let alive = true;
    setData(null);
    api(`/api/cases/${caseId}${mode === 'live' ? '?mode=live' : ''}`)
      .then((c) => alive && (setData(c), setErr(null)))
      .catch((e) => alive && setErr(e));
    api(`/api/cases/${caseId}/audit`).then((a) => alive && setAudit(a)).catch(() => {});
    return () => { alive = false; };
  }, [caseId, mode]);

  if (err) return <main className="page"><ErrorBox e={err} text="案例数据加载失败" /></main>;
  if (!data) return <main className="page"><LoadingBox text="加载案例…" /></main>;

  const isPR2 = data.case_id === 'pr2-high-risk-human-gate';
  const isPR3 = data.case_id === 'pr3-high-risk-human-reject';
  const hasGate = isPR2 || isPR3;
  const liveInfo = data.live;
  const crumb = [
    { icon: <IconBranch size={13} />, label: data.breadcrumb.repository, title: '仓库' },
    { icon: <IconPr size={13} />, label: data.breadcrumb.pull_request, title: 'Pull Request' },
    { icon: <IconBox size={13} />, label: data.breadcrumb.project, title: 'AgentTeams Project' },
    { icon: <IconUsers size={13} />, label: data.breadcrumb.team, title: 'Team' },
    { icon: <IconGear size={13} />, label: data.breadcrumb.runtime, title: 'Runtime' },
  ];

  return (
    <main className="page">
      <div className="case-rail" aria-label="链路与视图">
        {crumb.map((c, i) => (
          <React.Fragment key={c.title}>
            {i > 0 && <span className="arrow">→</span>}
            <span className="seg" title={c.title}>{c.icon}{c.label}</span>
          </React.Fragment>
        ))}
        <span className="spacer" />
        <span className={`chip ${view === 'presentation' ? 'info' : 'off'}`}><IconStage size={12} />{VIEW_LABEL[view]} MODE</span>
        <ModeBanner modes={modes} mode={mode} />
      </div>

      {mode === 'live' && (
        <div className={`statebox ${liveInfo?.available ? '' : 'error'}`} role="status" style={{ marginTop: 12 }}>
          {liveInfo?.available
            ? <>LIVE — CONNECTED TO AGENTTEAMS<span className="mono">{Object.entries(liveInfo.sources || {}).map(([k, v]) => `${k}:${v.status}`).join(' · ')}{liveInfo.github_pr?.available ? ` · github PR#${liveInfo.github_pr.number}: ${liveInfo.github_pr.state}` : ''}</span></>
            : <>LIVE DATA UNAVAILABLE<span className="mono">{liveInfo?.note}</span></>}
        </div>
      )}

      {view === 'operations' && (
        <div className="view-note" style={{ margin: '4px 0 12px' }}>
          <span className="chip off">OPERATIONS — 研发控制台：真实项目状态 · 任务表 · 数据源 · 健康与写操作策略</span>
        </div>
      )}
      {view === 'presentation' && (
        <div className="view-note" style={{ margin: '4px 0 12px' }}>
          <span className="chip info">PRESENTATION MODE</span>
          <span className="chip info">REPLAY — HISTORICAL VERIFIED RUN</span>
          <button className="linklike" onClick={() => setDrawer(hasGate ? { kind: 'finding' } : { kind: 'task', task: 'review-1' })}><IconCheck size={12} /> Evidence — 打开本案证据</button>
          <span className="faint small">用于答辩追问：事件列表 · Matrix event · result.md · Patch · Test output · Trace ID · SHA256 · 时间精度 · 来源</span>
        </div>
      )}
      {view === 'evidence' && (
        <div className="view-note" style={{ margin: '4px 0 12px' }}>
          <span className="chip ok">EVIDENCE — 数据从哪里来、为什么可信：字段级来源 · 哈希校验 · 时间精度</span>
        </div>
      )}

      {view === 'presentation' ? (
        <>
          <section aria-label="DAG 舞台" style={{ marginTop: 4 }}>
            <Stage caseData={data} at={idx} playing={playing} onTogglePlay={() => setPlaying(!playing)} />
          </section>
          <Filmstrip caseId={caseId} idx={idx} />
        </>
      ) : (
        <section className="sec" style={{ borderTop: 'none', marginTop: 8, paddingTop: 0 }}>
          {view === 'operations' && <OperationsView caseData={data} openDrawer={setDrawer} />}
          {view === 'evidence' && <EvidenceView caseData={data} openDrawer={setDrawer} />}
        </section>
      )}

      {view === 'presentation' && hasGate && (
        <section className="sec" aria-label="高危人工门">
          <div className="sec-head">
            <h2><IconGate size={15} /> Human Gate — 人工安全门</h2>
            <span className="sub">{isPR3 ? '拒绝路径：HUMAN_SECURITY_REJECTED · Fixer/Verifier 锁定 · 项目 blocked' : '批准路径：审批记录（真实人工审批）'}</span>
          </div>
          <GatePanel caseData={data} at={idx} openDrawer={setDrawer} />
        </section>
      )}

      {view === 'presentation' && isPR2 && data.fix_comparison && (
        <section className="sec" aria-label="修复对比">
          <div className="sec-head">
            <h2>Fix Before / After — 修复对比</h2>
            <span className="sub">探针数据来自 Verifier 独立采集的原始输出</span>
          </div>
          <ProbeCompare fc={data.fix_comparison} />
        </section>
      )}

      {view !== 'evidence' && (
        <section className="sec" aria-label="案例事实">
          <div className="sec-head">
            <h2>Case Facts — 项目与 PR</h2>
            <span className="sub">切换到 Evidence 视图查看字段级来源</span>
          </div>
          <div className="grid2">
            <div className="panel">
              <dl className="kv">
                <dt>案例</dt><dd><b>{data.title}</b><br /><span className="muted small">{data.positioning}</span></dd>
                <dt>项目</dt><dd className="mono">{data.project.id} — {data.project.status}</dd>
                <dt>风险</dt><dd>{data.risk.level === 'high' ? <span className="chip bad">HIGH · {data.risk.category}</span> : data.risk.level === 'critical' ? <span className="chip bad">CRITICAL · {data.risk.category} · REJECTED</span> : <span className="chip ok">NORMAL</span>}</dd>
              </dl>
            </div>
            <div className="panel">
              <dl className="kv">
                <dt>仓库 / PR</dt><dd className="mono small break">{data.repository} · #{data.pull_request.number} ({data.pull_request.branch})</dd>
                <dt>PR 状态</dt><dd><span className="chip warn">{String(data.pull_request.state).toUpperCase()}</span> <span className="faint small">{data.pull_request.state_note}</span></dd>
                <dt>写操作</dt><dd className="mono small">write_actions: {String(data.pull_request.write_actions)} — 未 merge/push/close/reopen</dd>
              </dl>
            </div>
          </div>
          {data.context_events?.length > 0 && view === 'presentation' && (
            <div className="panel" style={{ marginTop: 14 }}>
              <h3 style={{ margin: '0 0 8px', fontSize: 13.5, color: 'var(--ink)' }}>案例前置历史（真实证据）</h3>
              <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                {data.context_events.map((e, i) => (
                  <li key={i} style={{ marginBottom: 6 }}>
                    <span className="mono faint">{e.timestamp} ({e.timestamp_precision})</span> — {e.summary}
                    <div className="src">{e.source_ref}</div>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      <section className="sec" aria-label="证据来源">
        <div className="sec-head">
          <h2><IconDoc size={15} /> Sources — 证据来源</h2>
          <span className="sub">原始证据目录只读，本平台不修改任何证据文件</span>
        </div>
        <div className="panel">
          <dl className="kv small">
            {Object.entries(data.sources).map(([k, v]) => (
              <React.Fragment key={k}>
                <dt>{k}</dt><dd><SourceRef>{v}</SourceRef></dd>
              </React.Fragment>
            ))}
          </dl>
        </div>
      </section>

      {drawer && <EvidenceDrawerLazy caseId={caseId} request={drawer} onClose={() => setDrawer(null)} />}
    </main>
  );
}

function EvidenceDrawerLazy(props) {
  const [Comp, setComp] = useState(null);
  useEffect(() => { import('../components/EvidenceDrawer.jsx').then((m) => setComp(() => m.default)); }, []);
  return Comp ? <Comp {...props} /> : null;
}
