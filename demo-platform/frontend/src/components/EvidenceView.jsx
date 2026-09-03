// frontend/src/components/EvidenceView.jsx — "数据从哪里来、为什么可信" view.
// Every important claim rendered with SOURCE / SOURCE REF / TIMESTAMP PRECISION
// and one click from its evidence drawer.
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { useDemo } from '../store.jsx';
import { LoadingBox, ErrorBox, StatusPill, SourceRef } from './ui.jsx';
import { SourceBadge, PrecisionBadge } from './EvidenceDrawer.jsx';
import { IconCheck, IconAlert, IconHash } from '../icons.jsx';

export default function EvidenceView({ caseData, openDrawer }) {
  const { timeline, idx, health } = useDemo();
  const [tasks, setTasks] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api(`/api/cases/${caseData.case_id}/tasks`).then((r) => { setTasks(r); setErr(null); }).catch(setErr);
  }, [caseData.case_id]);

  if (err) return <ErrorBox e={err} text="Evidence 数据加载失败" />;
  if (!tasks || !timeline) return <LoadingBox text="加载证据视图…" />;

  const current = timeline.events[idx - 1] ?? null;
  const g = caseData.gate;

  return (
    <div>
      {caseData.risk.level === 'high' && (
        <div className="panel" style={{ borderColor: 'rgba(251,191,36,0.4)' }}>
          <div className="sec-head" style={{ marginBottom: 8 }}>
            <h2 style={{ color: 'var(--amber)' }}><IconAlert size={15} /> Finding — HIGH RISK FOUND · {caseData.risk.category}</h2>
          </div>
          <dl className="kv small">
            <dt>结论</dt><dd className="mono small">{caseData.risk.reviewer_conclusion}</dd>
            <dt>受影响文件</dt><dd className="mono small break">{caseData.risk.affected_file}</dd>
          </dl>
          <button className="linklike" onClick={() => openDrawer({ kind: 'finding' })}>打开 Finding 证据（含探针）→</button>
        </div>
      )}

      <section className="sec" style={{ marginTop: 16, paddingTop: 0 }}>
        <div className="sec-head">
          <h2>Claims — 字段级来源</h2>
          <span className="sub">每条断言 = value + SOURCE + SOURCE REF + TIMESTAMP PRECISION</span>
        </div>
        <div className="panel" style={{ padding: '6px 14px' }}>
          <ClaimRow label="PR state" value={`${caseData.pull_request.state.toUpperCase()}（write_actions: ${String(caseData.pull_request.write_actions)}）`} s={{ source: 'GitHub PR / Evidence Replay', source_ref: caseData.sources.pr_state, precision: null }} onOpen={() => openDrawer({ kind: 'task', task: caseData.tasks[0].id })} openLabel="案例任务证据 →" />
          <ClaimRow label="project status" value={caseData.project.status} s={tasks.tasks[0].source_map.status} onOpen={() => openDrawer({ kind: 'task', task: 'review-1' })} openLabel="review-1 证据 →" />
          <ClaimRow label={`review-1 result`} value={tasks.tasks.find((t) => t.id === 'review-1')?.result_status} s={tasks.tasks.find((t) => t.id === 'review-1')?.source_map.result} onOpen={() => openDrawer({ kind: 'task', task: 'review-1' })} openLabel="证据 →" />
          {caseData.fix_comparison && (
            <>
              <ClaimRow label="Fixer result" value={caseData.fix_comparison.fixer_result} s={tasks.tasks.find((t) => t.id === 'fix-1')?.source_map.result} onOpen={() => openDrawer({ kind: 'task', task: 'fix-1' })} openLabel="fix-1 证据 →" />
              <ClaimRow label="Verifier result" value={caseData.fix_comparison.verifier_result} s={tasks.tasks.find((t) => t.id === 'verify-1')?.source_map.result} onOpen={() => openDrawer({ kind: 'task', task: 'verify-1' })} openLabel="verify-1 证据 →" />
              <ClaimRow label="probe" value={`越界 ${caseData.fix_comparison.probe.before.traversal_status} → ${caseData.fix_comparison.probe.after.traversal_status} · 合法 ${caseData.fix_comparison.probe.before.legit_status} → ${caseData.fix_comparison.probe.after.legit_status}`} s={{ source: 'MinIO task result', source_ref: caseData.sources.probe_raw, precision: null }} onOpen={() => openDrawer({ kind: 'finding' })} openLabel="探针证据 →" />
            </>
          )}
          {g.state !== 'none' && (
            <ClaimRow label="human approval" value={`${g.state.toUpperCase()} by ${g.approver}`} s={{ source: 'Evidence Replay', source_ref: g.record_path, precision: 'approx' }} onOpen={() => openDrawer({ kind: 'approval' })} openLabel="审批记录证据 →" />
          )}
          <ClaimRow label="trace" value={`Trace Mode: ${caseData.trace_status.trace_mode.toUpperCase()} · Cloud Trace: Smoke ${caseData.trace_status.smoke?.status} / Live CoPaw Run ${caseData.trace_status.live_copaw_run?.status}`} s={{ source: 'AgentLoop Cloud', source_ref: caseData.trace_status.schema_only.path, precision: null }} onOpen={() => openDrawer({ kind: 'trace' })} openLabel="trace 证据 →" />
          <ClaimRow label="evidence integrity" value={`SHA256 ${health?.integrity?.ok_files}/${health?.integrity?.total_files} VERIFIED（启动时重算）`} s={{ source: 'Evidence Replay', source_ref: 'SHA256SUMS per evidence dir', precision: null }} onOpen={() => openDrawer({ kind: 'trace' })} openLabel="校验说明 →" />
        </div>
      </section>

      <section className="sec">
        <div className="sec-head">
          <h2>Timeline Evidence — 全部事件（可追溯）</h2>
          <span className="sub">{timeline.total_events} 条 · 每条含 event_id / source / 精度</span>
        </div>
        <div className="panel" style={{ padding: 0, overflowX: 'auto' }}>
          <table className="ops-table">
            <thead>
              <tr><th>#</th><th>Timestamp</th><th>Precision</th><th>Role</th><th>Type</th><th>Source</th><th>Summary</th><th>Evidence</th></tr>
            </thead>
            <tbody>
              {timeline.events.map((e) => (
                <tr key={e.event_id} className={`clickrow ${e.seq === idx ? 'current-row' : ''}`}>
                  <td className="mono">{String(e.seq).padStart(2, '0')}</td>
                  <td className="mono small">{e.timestamp}</td>
                  <td><PrecisionBadge precision={e.timestamp_precision} note={e.timestamp_note} /></td>
                  <td className="mono small">{e.agent_role}</td>
                  <td className="mono small">{e.event_type}</td>
                  <td><SourceBadge label={{ matrix: 'Matrix event', minio: 'MinIO task result', evidence: 'Evidence Replay', controller: 'Controller API' }[e.source]} /></td>
                  <td className="small" style={{ maxWidth: 340 }}>{e.summary}</td>
                  <td><button className="linklike" onClick={() => openDrawer({ kind: 'event', seq: e.seq })}>证据 →</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

function ClaimRow({ label, value, s, onOpen, openLabel }) {
  return (
    <div className="claim-row">
      <span className="claim-label mono">{label}</span>
      <span className="claim-value">
        <b className="small">{String(value)}</b>
        <span className="claim-src">
          <SourceBadge label={s.source} />
          {s.precision && <PrecisionBadge precision={s.precision} />}
        </span>
        <span className="src small"><IconHash size={11} />{s.source_ref}</span>
      </span>
      <button className="linklike" onClick={onOpen}>{openLabel}</button>
    </div>
  );
}
