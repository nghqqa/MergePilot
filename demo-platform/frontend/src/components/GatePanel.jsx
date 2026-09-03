// frontend/src/components/GatePanel.jsx — gate detail with PRESENT-state semantics
// (CREDIBILITY-PASS §6): before gate LOCKED×2 + PR writes disabled; after approval
// Approval event/by/at PRESENT, Fixer UNLOCKED, Verifier WAITING_FOR_FIX.
import React, { useEffect, useState, useCallback } from 'react';
import { api } from '../api.js';
import { useDemo } from '../store.jsx';
import { LoadingBox, ErrorBox, StatusPill, SourceRef } from './ui.jsx';
import { IconLock, IconUnlock, IconCheck, IconAlert } from '../icons.jsx';

export default function GatePanel({ caseData, at, openDrawer }) {
  const caseId = caseData.case_id;
  const { mode } = useDemo();
  const [appr, setAppr] = useState(null);
  const [dag, setDag] = useState(null);
  const [err, setErr] = useState(null);

  const load = useCallback(() => {
    api(`/api/cases/${caseId}/approval`).then((r) => { setAppr(r); setErr(null); }).catch(setErr);
  }, [caseId]);
  useEffect(load, [load]);

  useEffect(() => {
    api(`/api/cases/${caseId}/dag?at=${at}`).then(setDag).catch(() => {});
  }, [caseId, at]);

  if (err) return <ErrorBox e={err} text="审批记录加载失败" />;
  if (!appr) return <LoadingBox text="加载人工门记录…" />;

  const gateNow = dag?.nodes?.find((n) => n.kind === 'gate')?.state ?? 'none';
  const fixer = dag?.nodes?.find((n) => n.id === 'fix-1');
  const verifier = dag?.nodes?.find((n) => n.id === 'verify-1');
  const hist = appr.historical_record;
  const approved = gateNow === 'approved';
  const rejected = gateNow === 'rejected';
  const riskCat = caseData.risk?.category ?? 'HIGH RISK';
  const riskLvl = String(caseData.risk?.level ?? '').toUpperCase();

  return (
    <div>
      <div className="gate-banner" role="region" aria-label="高危横幅">
        <div className="g1"><IconAlert /> HIGH RISK FOUND — {riskCat} ({riskLvl})</div>
        <div className="g2">HUMAN VERIFICATION REQUIRED — 人工确认后方可修复 · PR write actions: DISABLED</div>
      </div>

      <div className="grid2">
        <div className="panel">
          <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>门控状态（当前回放位置）</h3>
          <div className="gate-row">
            <span className="g-label">人工门</span>
            <span className="g-value">
              <StatusPill status={approved ? 'completed' : gateNow === 'required' ? 'waiting_human' : rejected ? 'rejected' : 'pending'} label={`GATE: ${gateNow.toUpperCase()}`} />
            </span>
          </div>
          <div className="gate-row">
            <span className="g-label">Fixer</span>
            <span className="g-value" style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              {fixer?.status === 'waiting_human' || fixer?.locked_reason ? <IconLock size={13} /> : <IconUnlock size={13} />}
              {fixer?.status === 'rejected' ? `LOCKED — ${fixer.locked_reason || '人工拒绝，永不派发'}`
                : fixer?.status === 'waiting_human' ? 'LOCKED — 等待人工批准'
                : fixer?.locked_reason ? `LOCKED — ${fixer.locked_reason}` : 'UNLOCKED — 已批准，允许启动/已运行'}
            </span>
          </div>
          <div className="gate-row">
            <span className="g-label">Verifier</span>
            <span className="g-value" style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              {verifier?.status === 'waiting_human' || verifier?.locked_reason ? <IconLock size={13} /> : <IconUnlock size={13} />}
              {verifier?.status === 'locked' ? `LOCKED — ${verifier.locked_reason || '永久锁定，不可派发'}`
                : verifier?.status === 'waiting_human' ? 'LOCKED — 人工门未批'
                : verifier?.status === 'blocked' || verifier?.locked_reason ? `WAITING_FOR_FIX — ${verifier.locked_reason}` : approved ? 'UNLOCKED — WAITING_FOR_FIX 已解除（fix-1 完成）' : 'UNLOCKED'}
            </span>
          </div>
          <div className="gate-row">
            <span className="g-label">PR write actions</span>
            <span className="g-value"><span className="chip bad">DISABLED — merge/push/close/reopen 全禁，PR 保持 OPEN</span></span>
          </div>
        </div>

        {rejected ? (
          <div className="panel">
            <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>拒绝后状态（历史真实记录 — 系统没有继续执行）</h3>
            <div className="gate-row">
              <span className="g-label">Decision</span>
              <span className="g-value"><span className="chip bad">HUMAN_SECURITY_REJECTED</span></span>
            </div>
            <div className="gate-row">
              <span className="g-label">Rejected by</span>
              <span className="g-value">{hist.rejected_by ?? 'operator (runtime owner) — 人工决定'}</span>
            </div>
            <div className="gate-row">
              <span className="g-label">Rejected at</span>
              <span className="g-value mono small">{hist.rejected_at ?? '—'}</span>
            </div>
            <div className="gate-row">
              <span className="g-label">Fixer</span>
              <span className="g-value"><span className="chip bad">LOCKED — REJECTED, never dispatch</span></span>
            </div>
            <div className="gate-row">
              <span className="g-label">Verifier</span>
              <span className="g-value"><span className="chip bad">LOCKED — 永久锁定（无修复可验证）</span></span>
            </div>
            <div className="gate-row">
              <span className="g-label">Project</span>
              <span className="g-value"><span className="chip bad">BLOCKED — paused（NOT completed）</span></span>
            </div>
            <div className="gate-row">
              <span className="g-label">批准成功显示</span>
              <span className="g-value"><span className="chip off">禁止 — 已拒绝案例不显示"批准成功"；恢复需新的真实人工批准事件</span></span>
            </div>
            {openDrawer && (
              <button className="linklike" onClick={() => openDrawer({ kind: 'approval' })}>打开拒绝记录证据（含 SHA256 校验）→</button>
            )}
          </div>
        ) : (
        <div className="panel">
          <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>批准后状态（历史真实记录）</h3>
          <div className="gate-row">
            <span className="g-label">Approval event</span>
            <span className="g-value">{approved ? <span className="chip ok"><IconCheck size={11} />PRESENT</span> : <span className="chip off">ABSENT — 未批准</span>}</span>
          </div>
          <div className="gate-row">
            <span className="g-label">Approved by</span>
            <span className="g-value">{approved ? hist.approver : '—'}</span>
          </div>
          <div className="gate-row">
            <span className="g-label">Approved at</span>
            <span className="g-value mono small">{approved ? hist.approved_at : '—'}</span>
          </div>
          <div className="gate-row">
            <span className="g-label">Fixer</span>
            <span className="g-value">{approved ? <span className="chip ok">UNLOCKED（授权派发 fix-1）</span> : '—'}</span>
          </div>
          <div className="gate-row">
            <span className="g-label">Verifier</span>
            <span className="g-value">{approved ? <span className="chip warn">WAITING_FOR_FIX（fix-1 完成后派发）</span> : '—'}</span>
          </div>
          {openDrawer && (
            <button className="linklike" onClick={() => openDrawer({ kind: 'approval' })}>打开审批记录证据（含 SHA256 校验）→</button>
          )}
        </div>
        )}
      </div>

      <div className="panel" style={{ marginTop: 14 }}>
        <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>风险详情与Reviewer 结论</h3>
        <dl className="kv">
          <dt>受影响文件</dt><dd className="mono small break">{caseData.risk.affected_file}</dd>
          <dt>风险说明</dt><dd className="muted small">{caseData.risk.description}</dd>
          <dt>Reviewer 结论</dt><dd className="mono small">{caseData.risk.reviewer_conclusion}</dd>
          {caseData.risk.residual_after_fix ? (<><dt>修复后残余</dt><dd>{caseData.risk.residual_after_fix}</dd></>) : null}
          {caseData.risk.no_fix_note ? (<><dt>修复说明</dt><dd className="muted small">{caseData.risk.no_fix_note}</dd></>) : null}
        </dl>
        <p className="faint small" style={{ marginBottom: 0 }}>
          审批操作位于上方舞台接管层（Presentation 视图，回放到达人工门时出现）。
          {mode === 'replay' ? ' Replay 模式按钮只修改回放状态（REPLAY ACTION — NO RUNTIME WRITE）。' : ' Live 模式审批只能调用受保护 Demo Backend 接口；未配置时显示 LIVE APPROVAL UNAVAILABLE，绝不显示批准成功。'}
        </p>
      </div>

      <div className="panel" style={{ marginTop: 14 }}>
        <h3 style={{ marginTop: 0, fontSize: 13.5, color: 'var(--ink)' }}>历史审批记录（真实证据，只读 — 非前端伪造，非 Agent 行为）</h3>
        <dl className="kv">
          <dt>gate id</dt><dd className="mono small">{hist.gate_id}</dd>
          <dt>触发</dt><dd className="small">{hist.trigger?.by} @ <span className="mono">{hist.trigger?.triggered_at}</span> — <span className="mono">{hist.trigger?.signals?.join(' / ')}</span></dd>
          <dt>授权范围</dt><dd className="small">{hist.scope?.join('；')}</dd>
          <dt>禁令</dt><dd className="mono small" style={{ color: 'var(--red)' }}>{hist.prohibitions?.join(' / ')}</dd>
          <dt>记录路径</dt><dd className="mono small break">{hist.record_path}</dd>
          {hist.terminal_note ? (<><dt>终态规则</dt><dd className="small">{hist.terminal_note}</dd></>) : null}
        </dl>
        <SourceRef>{rejected ? appr.rejection_text_source : appr.approval_text_source}</SourceRef>
      </div>
    </div>
  );
}
