// frontend/src/components/OperationsView.jsx — the研发控制台 view (default entry).
// Compact truth: project/pr/risk/task/agents/DAG-table/data sources/last event/
// trace status/PR write policy/health. Everything opens the evidence drawer.
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { useDemo } from '../store.jsx';
import { LoadingBox, ErrorBox, StatusPill, SourceRef } from './ui.jsx';
import { SourceBadge, PrecisionBadge } from './EvidenceDrawer.jsx';
import { IconLock, IconUnlock, IconGate, IconBranch, IconHash, IconDoc, IconAlert, IconCheck } from '../icons.jsx';

export default function OperationsView({ caseData, openDrawer }) {
  const { health, idx, timeline } = useDemo();
  const [tasks, setTasks] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api(`/api/cases/${caseData.case_id}/tasks`).then((r) => { setTasks(r); setErr(null); }).catch(setErr);
  }, [caseData.case_id]);

  if (err) return <ErrorBox e={err} text="Operations 数据加载失败" />;
  if (!tasks) return <LoadingBox text="加载控制台…" />;

  const lastEvent = timeline?.events?.[Math.max(0, (idx || timeline?.total_events || 1) - 1)];
  const isPR2 = caseData.case_id === 'pr2-high-risk-human-gate';

  return (
    <div>
      <section className="ops-grid" aria-label="Operations 概览">
        <div className="panel ops-cell">
          <h3 className="ops-h">Project / PR</h3>
          <dl className="kv small">
            <dt>project</dt><dd className="mono">{caseData.project.id} — <b>{caseData.project.status}</b></dd>
            <dt>pull request</dt><dd className="mono">#{caseData.pull_request.number} · {caseData.pull_request.branch}</dd>
            <dt>PR state</dt><dd><span className="chip warn">{String(caseData.pull_request.state).toUpperCase()}</span></dd>
            <dt>PR write policy</dt><dd><span className="chip bad">write_actions: {String(caseData.pull_request.write_actions)} — merge/push/close/reopen 禁止</span></dd>
          </dl>
        </div>
        <div className="panel ops-cell">
          <h3 className="ops-h">Risk / Gate</h3>
          <dl className="kv small">
            <dt>risk</dt><dd>{caseData.risk.level === 'high'
              ? <span className="chip bad">HIGH · {caseData.risk.category}</span>
              : <span className="chip ok">NORMAL — 非高危</span>}</dd>
            <dt>human gate</dt><dd><StatusPill status={caseData.gate.state === 'approved' ? 'completed' : caseData.gate.state === 'required' ? 'waiting_human' : 'pending'} label={`GATE: ${caseData.gate.state.toUpperCase()}`} /></dd>
            <dt>residual</dt><dd className="muted small">{caseData.risk.residual_after_fix ?? '—'}</dd>
            {isPR2 && <dt>finding</dt>}
            {isPR2 && <dd><button className="linklike" onClick={() => openDrawer({ kind: 'finding' })}>打开 Finding 证据 →</button></dd>}
          </dl>
        </div>
        <div className="panel ops-cell">
          <h3 className="ops-h">Team / Runtime</h3>
          <dl className="kv small">
            <dt>team</dt><dd className="mono">{caseData.breadcrumb.team}</dd>
            <dt>agents</dt><dd>{caseData.agents.map((a) => <span key={a.role} className="chip" style={{ margin: '0 4px 4px 0' }}>{a.role} ×1</span>)}</dd>
            <dt>runtime</dt><dd className="mono">{caseData.runtime.worker_image}</dd>
            <dt>stack</dt><dd className="mono small">{caseData.runtime.stack}</dd>
          </dl>
        </div>
        <div className="panel ops-cell">
          <h3 className="ops-h">Health / Trace</h3>
          <dl className="kv small">
            <dt>evidence</dt><dd><span className="chip ok">Final Package: {health?.integrity?.final_submission_integrity?.status ?? '…'}</span> <span className="chip warn">历史材料: {health?.integrity?.historical_source_integrity?.status ?? '…'}</span></dd>
            <dt>trace mode</dt><dd><span className="chip warn">{caseData.trace_status.status}</span></dd>
            <dt>cloud trace</dt><dd><span className="chip ok">Smoke: {caseData.trace_status.smoke?.status ?? '…'}</span> <span className="chip warn">Live CoPaw Run: {caseData.trace_status.live_copaw_run?.status ?? '…'}</span></dd>
            <dt></dt><dd><button className="linklike" onClick={() => openDrawer({ kind: 'trace' })}>Trace 证据详情 →</button></dd>
          </dl>
        </div>
      </section>

      <section className="sec" style={{ marginTop: 18, paddingTop: 16 }}>
        <div className="sec-head">
          <h2><IconBranch size={15} /> Task Table — DAG</h2>
          <span className="sub">状态真实映射 · 点击行打开证据抽屉</span>
        </div>
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="ops-table">
            <thead>
              <tr><th>Task</th><th>Agent</th><th>Status</th><th>Locked</th><th>Upstream</th><th>Started</th><th>Finished</th><th>Source</th><th>Result</th><th>Evidence</th></tr>
            </thead>
            <tbody>
              {tasks.tasks.map((t) => (
                <tr key={t.id} className="clickrow" onClick={() => openDrawer({ kind: 'task', task: t.id })} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && openDrawer({ kind: 'task', task: t.id })} aria-label={`打开 ${t.id} 证据`}>
                  <td className="mono"><b>{t.id}</b></td>
                  <td className="mono small">{t.assignee}</td>
                  <td><StatusPill status={t.final_status.status} label={t.final_status.status_label ?? t.final_status.status} /></td>
                  <td>{t.locked
                    ? <span className="chip bad" title={t.locked_reason || ''}><IconLock size={11} />LOCKED</span>
                    : <span className="chip ok"><IconUnlock size={11} />UNLOCKED</span>}</td>
                  <td className="mono small">{t.upstream.length ? t.upstream.join(', ') : '—'}</td>
                  <td className="mono small">{t.started_at}<PrecisionBadge precision={t.source_map?.started_at?.timestamp_precision} /></td>
                  <td className="mono small">{t.ended_at}<PrecisionBadge precision={t.ended_at_precision} /></td>
                  <td><SourceBadge label={t.source_map?.result?.source} /></td>
                  <td className="mono small">{t.result_status}{t.effective ? ' · effective' : ''}</td>
                  <td><button className="linklike" onClick={(e) => { e.stopPropagation(); openDrawer({ kind: 'task', task: t.id }); }}>证据 →</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <SourceRef>状态由证据时间线 delta 计算（GET /api/cases/{caseData.case_id}/tasks），每行 source_map 携带 SOURCE / SOURCE REF</SourceRef>
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2><IconDoc size={15} /> Last Event — 最近事件</h2>
          <span className="sub">回放位置 {idx}/{timeline?.total_events ?? '…'}</span>
        </div>
        {lastEvent && (
          <div className="panel">
            <div className="tl-head">
              <span className="tl-ts">{lastEvent.timestamp}</span>
              <PrecisionBadge precision={lastEvent.timestamp_precision} note={lastEvent.timestamp_note} />
              <SourceBadge label={{ matrix: 'Matrix event', minio: 'MinIO task result', evidence: 'Evidence Replay', controller: 'Controller API' }[lastEvent.source]} />
            </div>
            <p className="tl-sum">{lastEvent.summary}</p>
            <div className="tl-meta">
              <span className="chip">{lastEvent.event_id}</span>
              {lastEvent.matrix_event_id && <span className="chip info">matrix: {lastEvent.matrix_event_id.slice(0, 24)}…</span>}
            </div>
            <div style={{ marginTop: 8 }}><button className="linklike" onClick={() => openDrawer({ kind: 'event', seq: lastEvent.seq })}>打开事件证据 →</button></div>
          </div>
        )}
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2><IconHash size={14} /> Data Sources — 数据源</h2>
        </div>
        <div className="panel" style={{ padding: '4px 14px' }}>
          {caseData.data_sources.map((s) => (
            <div className="audit-line" key={s.label}>
              <span className="chip info" style={{ minWidth: 150, justifyContent: 'center' }}>{s.label}</span>
              <span className="why small">{s.status}<br /><SourceRef>{s.ref}</SourceRef></span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
