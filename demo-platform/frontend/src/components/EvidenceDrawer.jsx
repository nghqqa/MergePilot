// frontend/src/components/EvidenceDrawer.jsx — the credibility layer.
// One drawer for event / task / approval / trace / finding payloads.
// Shows SOURCE / SOURCE REF / TIMESTAMP PRECISION / HASH VERIFIED / REDACTION for
// every claim; forbidden credential fields never exist in the payload (selftest-enforced).
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox, SourceRef, StatusPill } from './ui.jsx';
import { IconX, IconHash, IconCheck, IconAlert, IconLock } from '../icons.jsx';

const SRC_CLS = {
  'Controller API': 'info', 'Matrix event': 'ok', 'MinIO task result': 'warn',
  'GitHub PR': 'info', 'Evidence Replay': 'info', 'AgentLoop Cloud': 'off',
};

export function SourceBadge({ label }) {
  if (!label) return null;
  return <span className={`chip ${SRC_CLS[label] ?? 'off'}`} title="数据来源">SOURCE: {label}</span>;
}

export function PrecisionBadge({ precision, note }) {
  if (!precision) return null;
  return <span className="chip off" title={note || ''}>PRECISION: {precision}</span>;
}

function HashRow({ h }) {
  if (!h) return null;
  return (
    <div className="hash-block">
      <div className="hash-line">
        <span className="faint small">证据文件</span>
        <span className={`chip ${h.exists ? (h.hash_verified === true ? 'ok' : h.hash_verified === false ? 'bad' : 'off') : 'bad'}`}>
          {h.exists ? (h.hash_verified === true ? <><IconCheck size={11} /> SHA256 VERIFIED</> : h.hash_verified === false ? 'SHA256 MISMATCH' : 'SHA256 RECOMPUTED (无 SUMS 条目)') : 'FILE MISSING'}
        </span>
      </div>
      {h.exists && <div className="mono small break">{h.dir}/{h.file}</div>}
      {h.sha256_actual && <div className="mono faint small break">sha256: {h.sha256_actual}</div>}
      <SourceRef>{h.sums_source}</SourceRef>
    </div>
  );
}

function RedactionRow({ rs }) {
  if (!rs) return null;
  return (
    <div className="hash-block">
      <div className="hash-line">
        <span className="faint small">脱敏状态</span>
        <span className={`chip ${rs.status?.startsWith('CLEAN') ? 'ok' : 'bad'}`}>{rs.status}</span>
      </div>
      <div className="faint small">排除字段：{rs.forbidden_fields_excluded?.join(' · ')}</div>
    </div>
  );
}

export function FieldSource({ s, compact = false }) {
  if (!s) return null;
  return (
    <span className="fsourced">
      <SourceBadge label={s.source} />
      {!compact && s.source_ref && <span className="src small"><IconHash size={11} />{s.source_ref}</span>}
      {s.timestamp_precision && <PrecisionBadge precision={s.timestamp_precision} />}
    </span>
  );
}

export default function EvidenceDrawer({ caseId, request, onClose }) {
  // request: {kind:'event'|'task'|'approval'|'trace'|'finding', seq?, task?}
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    setData(null);
    const qs = new URLSearchParams({ kind: request.kind, ...(request.seq ? { seq: request.seq } : {}), ...(request.task ? { task: request.task } : {}) });
    api(`/api/cases/${caseId}/evidence?${qs}`).then((r) => alive && (setData(r), setErr(null))).catch((e) => alive && setErr(e));
    return () => { alive = false; };
  }, [caseId, request]);

  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="drawer" role="dialog" aria-label="证据抽屉">
        <button className="btn ghost close" onClick={onClose}><IconX />关闭</button>
        {err && <ErrorBox e={err} text="证据加载失败" />}
        {!err && !data && <LoadingBox text="加载证据…" />}
        {data && <DrawerBody d={data} onClose={onClose} />}
      </div>
    </div>
  );
}

function KVRow({ k, children }) {
  return <div className="gate-row"><span className="g-label">{k}</span><span className="g-value">{children}</span></div>;
}

function DrawerBody({ d }) {
  const isTrace = d.drawer === 'trace';
  return (
    <div>
      <h3 style={{ textTransform: 'uppercase', letterSpacing: '0.03em' }}>
        {d.drawer === 'event' && `Evidence · Event ${d.seq}`}
        {d.drawer === 'task' && `Evidence · Task ${d.task_id}`}
        {d.drawer === 'approval' && 'Evidence · Human Approval'}
        {d.drawer === 'trace' && 'Evidence · Trace Status'}
        {d.drawer === 'finding' && 'Evidence · Risk Finding'}
      </h3>

      <div className="panel" style={{ padding: '10px 14px' }}>
        <KVRow k="data_mode"><span className="mono small">{d.data_mode ?? 'historical replay'}</span></KVRow>
        <KVRow k="trace_mode"><span className="mono small">{d.trace_mode ?? 'evidence-replay'}</span></KVRow>
        <KVRow k="integrity_status"><span className={`chip ${(d.integrity_status||'').startsWith('drift_documented') ? 'warn' : 'ok'}`}>{d.integrity_status ?? 'verified'}</span></KVRow>
        <KVRow k="source"><SourceBadge label={d.source_label ?? SRC_LABEL_FALLBACK(d)} /></KVRow>
        <KVRow k="source_ref"><span className="mono small break">{d.source_ref}</span></KVRow>
        <KVRow k="timestamp">
          <span className="mono small">{d.timestamp}</span> <PrecisionBadge precision={d.timestamp_precision} note={d.timestamp_note} />
        </KVRow>
        {d.matrix_event_id && <KVRow k="matrix event"><span className="mono small break">{d.matrix_event_id}</span></KVRow>}
        <KVRow k="trace_id"><span className="mono small">{d.trace_id ?? 'null'}</span> <span className="faint small">{d.trace_note || 'AgentLoop 未接入 — 待接入'}</span></KVRow>
        {d.event_id && <KVRow k="event_id"><span className="mono small">{d.event_id}</span></KVRow>}
        {d.project_id && <KVRow k="project_id"><span className="mono small">{d.project_id}</span></KVRow>}
        {d.task_id !== undefined && d.task_id !== null && <KVRow k="task_id"><span className="mono small">{d.task_id}</span></KVRow>}
        {d.agent_role && <KVRow k="agent_role"><span className="mono small">{d.agent_role}</span></KVRow>}
        {d.runtime && <KVRow k="runtime"><span className="mono small">{d.runtime}</span></KVRow>}
        {d.event_type && <KVRow k="event_type"><span className="mono small">{d.event_type}</span></KVRow>}
      </div>

      {d.drawer === 'event' && (d.summary || d.detail) && (
        <div className="panel">
          <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>事件内容（脱敏后）</h3>
          <p className="small muted" style={{ marginTop: 0 }}>{d.summary}</p>
          {d.detail && <pre className="tl-detail">{d.detail}</pre>}
        </div>
      )}

      {d.drawer === 'task' && (
        <>
          <div className="panel">
            <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>结果协议（result.md 摘要）</h3>
            <KVRow k="result_status"><StatusPill status={d.result_status === 'SUCCESS' ? 'completed' : 'running'} label={d.result_status} /></KVRow>
            <KVRow k="effective"><span className="mono small">{String(d.effective)}</span>{d.effective_note ? <span className="faint small"> — {d.effective_note}</span> : null}</KVRow>
            <KVRow k="assignee"><span className="mono small">{d.assignee}</span></KVRow>
            {d.input_context && <KVRow k="输入上下文"><span className="muted small">{d.input_context}</span></KVRow>}
            {d.tool_calls_summary && <KVRow k="工具调用摘要"><span className="mono small">{d.tool_calls_summary}</span></KVRow>}
            <pre className="tl-detail">{d.result_summary}</pre>
          </div>
          <div className="panel">
            <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>产物与校验</h3>
            {d.artifacts.map((a) => (
              <div key={a.name} className="hash-block">
                <div className="hash-line">
                  <span className="mono small">{a.name}</span>
                  {a.resolved && <span className={`chip ${a.resolved.exists ? (a.resolved.hash_verified ? 'ok' : 'off') : 'bad'}`}>{a.resolved.exists ? (a.resolved.hash_verified ? 'SHA256 VERIFIED' : 'EXISTS') : 'MISSING'}</span>}
                </div>
                <SourceRef>{a.source}</SourceRef>
              </div>
            ))}
          </div>
          <div className="panel">
            <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>状态变更历史</h3>
            <ul className="hist">
              {d.status_history.map((h, i) => (
                <li key={i}><span>{h.at}</span><b className={h.status}>{h.status}</b><span>{h.locked_reason || ''}</span></li>
              ))}
            </ul>
          </div>
        </>
      )}

      {d.drawer === 'approval' && (
        <div className="panel">
          <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>批准记录（真实人工审批，只读）</h3>
          <KVRow k="gate id"><span className="mono small">{d.gate.gate_id}</span></KVRow>
          <KVRow k="审批人">{d.gate.approver}</KVRow>
          <KVRow k="审批时间"><span className="mono small">{d.gate.approved_at}</span></KVRow>
          <KVRow k="禁令"><span className="mono small" style={{ color: 'var(--red)' }}>{d.gate.prohibitions?.join(' / ')}</span></KVRow>
          {d.approval_text && <pre className="tl-detail">{d.approval_text}</pre>}
        </div>
      )}

      {isTrace && (
        <div className="panel">
          <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>Trace 状态（预留字段）</h3>
          <KVRow k="status"><span className="chip warn">{d.status}</span></KVRow>
          <KVRow k="Cloud Trace — smoke"><span className="chip ok">Smoke: {d.smoke?.status}</span></KVRow>
          <KVRow k="Cloud Trace — live run"><span className="chip warn">Live CoPaw Run: {d.live_copaw_run?.status}</span></KVRow>
          <KVRow k="trace_mode"><span className="mono small">{d.trace_mode}</span></KVRow>
          <KVRow k="data_mode"><span className="mono small">{d.data_mode}</span></KVRow>
          <KVRow k="cloud_visibility"><span className="mono small">{String(d.cloud_visibility)}</span></KVRow>
          <KVRow k="span_count"><span className="mono small">{String(d.span_count)}</span></KVRow>
          <KVRow k="trajectory_evaluation"><span className="mono small">{String(d.trajectory_evaluation)}</span></KVRow>
          <KVRow k="result_evaluation"><span className="mono small">{String(d.result_evaluation)}</span></KVRow>
          <p className="faint small">{d.reason}</p>
          <SourceRef>{d.schema_only?.path}</SourceRef>
        </div>
      )}

      {d.drawer === 'finding' && (
        <>
          <div className="panel">
            <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>Finding</h3>
            <KVRow k="severity"><span className="chip bad">HIGH · {d.finding.category}</span></KVRow>
            <KVRow k="受影响文件"><span className="mono small break">{d.finding.affected_file}</span></KVRow>
            <KVRow k="说明"><span className="muted small">{d.finding.description}</span></KVRow>
            <KVRow k="Reviewer 结论"><span className="mono small">{d.finding.reviewer_conclusion}</span></KVRow>
          </div>
          {d.probe && (
            <div className="panel">
              <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>探针对照（Verifier 独立采集）</h3>
              <table className="probe-table">
                <thead><tr><th>请求</th><th>修复前</th><th>修复后</th></tr></thead>
                <tbody>
                  <tr><td className="mono small">{d.probe.requests.traversal}</td><td><span className="code-200">{d.probe.before.traversal_status}</span></td><td><span className="code-404">{d.probe.after.traversal_status}</span></td></tr>
                  <tr><td className="mono small">{d.probe.requests.legit}</td><td><span className="code-200 ok">{d.probe.before.legit_status}</span></td><td><span className="code-200 ok">{d.probe.after.legit_status}</span></td></tr>
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      <div className="panel">
        <HashRow h={d.source_hash} />
        <RedactionRow rs={d.redaction_status} />
      </div>
    </div>
  );
}

function SRC_LABEL_FALLBACK(d) {
  return { controller: 'Controller API', matrix: 'Matrix event', minio: 'MinIO task result', evidence: 'Evidence Replay' }[d.source] ?? d.source;
}
