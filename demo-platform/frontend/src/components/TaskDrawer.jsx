// frontend/src/components/TaskDrawer.jsx — task detail drawer (all fields from backend)
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LoadingBox, ErrorBox, StatusPill, SourceRef } from './ui.jsx';

export default function TaskDrawer({ caseId, taskId, onClose }) {
  const [tasks, setTasks] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    api(`/api/cases/${caseId}/tasks`).then(setTasks).catch(setErr);
  }, [caseId]);

  useEffect(() => {
    const h = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  const t = tasks?.tasks?.find((x) => x.id === taskId);

  return (
    <div className="drawer-backdrop" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="drawer" role="dialog" aria-label={`任务详情 ${taskId}`}>
        <button className="btn ghost close" onClick={onClose}>关闭 ✕</button>
        {err && <ErrorBox e={err} />}
        {!err && !t && <LoadingBox />}
        {t && (
          <div>
            <h3>{t.id}</h3>
            <StatusPill status={t.final_status.status} label={t.final_status.status_label} />
            {t.final_status.locked_reason && <p className="small" style={{ color: 'var(--warn)' }}>🔒 {t.final_status.locked_reason}</p>}
            <div className="panel" style={{ marginTop: 14 }}>
              <dl className="kv">
                <dt>task id</dt><dd className="mono">{t.id}</dd>
                <dt>project id</dt><dd className="mono">{t.project_id}</dd>
                <dt>assignee</dt><dd className="mono">{t.assignee}</dd>
                <dt>runtime</dt><dd className="mono">{t.runtime}</dd>
                <dt>开始 / 结束</dt><dd className="mono">{t.started_at} → {t.ended_at} {t.ended_at_precision !== 'exact' && <span className="faint">({t.ended_at_precision})</span>}</dd>
                <dt>result_status</dt><dd className="mono">{t.result_status}</dd>
                <dt>effective</dt><dd>{String(t.effective)}{t.effective_note ? <span className="faint"> — {t.effective_note}</span> : null}</dd>
                <dt>trace id</dt><dd className="mono">{t.trace_id ?? 'null'} <span className="faint">（{t.trace_note}）</span></dd>
              </dl>
            </div>
            <div className="panel">
              <h3>输入上下文摘要</h3>
              <p className="small muted">{t.input_context}</p>
            </div>
            <div className="panel">
              <h3>工具调用摘要</h3>
              <p className="small muted mono">{t.tool_calls}</p>
            </div>
            <div className="panel">
              <h3>result.md 摘要</h3>
              <pre className="tl-detail" style={{ whiteSpace: 'pre-wrap' }}>{t.result_summary}</pre>
            </div>
            <div className="panel">
              <h3>产物</h3>
              <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
                {t.artifacts.map((a) => <li key={a.name}><span className="mono">{a.name}</span> <span className="faint">— {a.source}</span></li>)}
              </ul>
            </div>
            <div className="panel">
              <h3>状态变更历史</h3>
              <ul className="hist">
                {t.status_history.map((h, i) => (
                  <li key={i}>
                    <span>{h.at}</span>
                    <b className={h.status}>{h.status}</b>
                    <span>{h.locked_reason || ''}</span>
                    <span className="faint">{h.event_id}</span>
                  </li>
                ))}
              </ul>
              <SourceRef>{t.status_history_source}</SourceRef>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
