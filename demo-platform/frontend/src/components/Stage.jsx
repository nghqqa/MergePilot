// frontend/src/components/Stage.jsx — THE DAG STAGE (split-theater lead surface).
// Left: oversized phase word + headline + progress rail.
// Right: vertical DAG column (backend-computed states at the replay position).
// Gate takeover overlay when the gate is REQUIRED (the one authored moment).
import React, { useEffect, useState } from 'react';
import { api } from '../api.js';
import { useDemo } from '../store.jsx';
import { LoadingBox, ErrorBox, StatusPill } from './ui.jsx';
import { IconLock, IconAlert, IconCheck, IconGate, IconPlay, IconPause } from '../icons.jsx';

const PHASE_DISPLAY = {
  running: { word: 'RUN', cls: 'blue' },
  waiting_human: { word: 'HUMAN GATE', cls: 'amber' },
  fixing: { word: 'FIXING', cls: 'cyan' },
  verifying: { word: 'VERIFYING', cls: 'cyan' },
  completed: { word: 'COMPLETE', cls: 'green' },
  rejected: { word: 'REJECTED', cls: 'red' },
  blocked: { word: 'BLOCKED', cls: 'red' },
};

export default function Stage({ caseData, at, playing, onTogglePlay }) {
  const [dag, setDag] = useState(null);
  const [err, setErr] = useState(null);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    let alive = true;
    api(`/api/cases/${caseData.case_id}/dag?at=${at}`)
      .then((r) => alive && (setDag(r), setErr(null)))
      .catch((e) => alive && setErr(e));
    api(`/api/cases/${caseData.case_id}/timeline`)
      .then((r) => alive && setTotal(r.total_events))
      .catch(() => {});
    return () => { alive = false; };
  }, [caseData.case_id, at]);

  if (err) return <ErrorBox e={err} text="DAG 状态加载失败" />;
  if (!dag) return <div className="stage" style={{ minHeight: 330 }}><LoadingBox text="加载舞台状态…" /></div>;

  const phase = PHASE_DISPLAY[dag.phase] ?? { word: dag.phase?.toUpperCase() || '—', cls: 'cyan' };
  const gateRequired = dag.nodes.some((n) => n.kind === 'gate' && n.state === 'required');
  const gateRejected = dag.nodes.some((n) => n.kind === 'gate' && n.state === 'rejected');
  const isRisk = caseData.risk.level === 'high' || caseData.risk.level === 'critical';
  const done = total > 0 ? Math.round((at / total) * 100) : 0;

  return (
    <div className={`stage ${isRisk ? 'risk' : ''}`}>
      <div className="stage-grid">
        <div className="stage-left">
          <h1 className={`phase-word ${phase.cls}`}>{phase.word}</h1>
          <p className="stage-headline">{dag.headline}</p>
          <div className="stage-meta">
            <span className="mono">{dag.at_event_seq === 0 ? 'INIT' : `EV ${String(dag.at_event_seq).padStart(2, '0')}`}/{total}</span>
            <span className="mono">{dag.timestamp || ''}</span>
            <div className={`progress-rail ${gateRequired ? 'gate' : ''}`} role="progressbar" aria-valuenow={done} aria-valuemin={0} aria-valuemax={100}>
              <i style={{ transform: `scaleX(${done / 100})` }} />
            </div>
            <button className="dbtn" onClick={onTogglePlay} title="播放/暂停 (Space)">
              {playing ? <IconPause /> : <IconPlay />}{playing ? '暂停' : '播放'}
            </button>
          </div>
        </div>

        <nav className="stage-dag" aria-label="DAG 节点列">
          <ol className="dag-col">
            {dag.nodes.map((n) => {
              const GateIcon = n.kind === 'gate' ? IconGate : null;
              const done = n.status === 'completed' || n.state === 'approved';
              return (
                <li key={n.id} className={`${n.kind === 'gate' ? `gate ${n.state}` : n.status}`}>
                  <span className="dag-node-dot">{GateIcon ? <GateIcon size={11} /> : done ? <IconCheck size={11} /> : null}</span>
                  <NodeRow node={n} caseId={caseData.case_id} />
                </li>
              );
            })}
          </ol>
        </nav>
      </div>

      {gateRequired && <Takeover caseData={caseData} dag={dag} />}
      {gateRejected && <RejectedBanner caseData={caseData} dag={dag} />}
    </div>
  );
}

function RejectedBanner({ caseData, dag }) {
  const fixer = dag.nodes.find((n) => n.id === 'fix-1');
  const verifier = dag.nodes.find((n) => n.id === 'verify-1');
  return (
    <div className="takeover entering" role="alertdialog" aria-label="人工拒绝后系统停止" style={{ borderColor: 'var(--red-deep, #b3261e)' }}>
      <div className="takeover-inner">
        <h2 className="takeover-title">HUMAN SECURITY REJECTED — 系统安全停止</h2>
        <p className="takeover-sub">
          {caseData.risk.category} · {String(caseData.risk.level).toUpperCase()} · 人工拒绝修复 — 没有继续执行
        </p>
        <p className="takeover-body">
          操作员正式拒绝（HUMAN_SECURITY_REJECTED，人工决定）。系统不绕过人工门：
          Fixer 与 Verifier 保持锁定，项目 BLOCKED，不执行任何自动修复。
          <br />
          <span className="faint">当前：Fixer {fixer ? `LOCKED（${fixer.locked_reason || 'REJECTED, never dispatch'}）` : ''}；Verifier {verifier ? `LOCKED（${verifier.locked_reason || 'locked'}）` : ''}。PR #{caseData.pull_request.number} 保持 OPEN（未修复）。等待未来新的人工批准事件（本案例不添加）。</span>
        </p>
        <div className="takeover-note">REPLAY ACTION — NO RUNTIME WRITE：历史拒绝为真实人工决定，回放仅展示，不写入 runtime</div>
      </div>
    </div>
  );
}

function NodeRow({ node, caseId }) {
  const [open, setOpen] = useState(false);
  const n = node;
  const body = (
    <>
      <div className="dag-row">
        <span className="dag-id">{n.label}</span>
        <span className="dag-sub">{n.sub}</span>
        <StatusPill status={n.status} label={n.status_label || n.status} />
      </div>
      {n.locked_reason && (
        <div className="dag-lock"><IconLock size={11} /> {n.locked_reason}</div>
      )}
    </>
  );
  if (n.kind === 'gate') return <div>{body}</div>;
  return (
    <>
      <button className="dag-node-btn" onClick={() => setOpen(true)} aria-haspopup="dialog" title="查看任务详情">
        {body}
      </button>
      {open && <TaskDrawerLazy caseId={caseId} taskId={n.id} onClose={() => setOpen(false)} />}
    </>
  );
}

function TaskDrawerLazy(props) {
  const [Comp, setComp] = useState(null);
  useEffect(() => {
    import('./TaskDrawer.jsx').then((m) => setComp(() => m.default));
  }, []);
  return Comp ? <Comp {...props} /> : null;
}

function Takeover({ caseData, dag }) {
  const gateNode = dag.nodes.find((n) => n.kind === 'gate');
  const fixer = dag.nodes.find((n) => n.id === 'fix-1');
  const verifier = dag.nodes.find((n) => n.id === 'verify-1');
  return (
    <div className="takeover entering" role="alertdialog" aria-label="人工安全门接管">
      <div className="takeover-inner">
        <h2 className="takeover-title"><IconAlert size={22} /> HIGH RISK FOUND — HUMAN GATE</h2>
        <p className="takeover-sub">{caseData.risk.category} {String(caseData.risk.level).toUpperCase()} · HUMAN VERIFICATION REQUIRED · 系统受控停等</p>
        <p className="takeover-body">
          Reviewer 确认高危（STATUS: HIGH_RISK_FOUND / SEVERITY: {String(caseData.risk.level).toUpperCase()} / HUMAN_VERIFICATION_REQUIRED: true）：
          Fixer 与 Verifier 全部锁定，Leader 禁止派发。人工决定可以是批准或拒绝 — 批准后继续修复与独立验证，拒绝后系统安全停止。
          <br />
          <span className="faint">当前：Fixer {fixer ? `LOCKED（${fixer.locked_reason || '等待人工决定'}）` : ''}；Verifier {verifier ? `LOCKED（${verifier.locked_reason || '等待人工决定'}）` : ''}。PR #{caseData.pull_request.number} 保持 OPEN。</span>
        </p>
        <GateActionsInline caseId={caseData.case_id} />
        <div className="takeover-note">REPLAY ACTION — NO RUNTIME WRITE：演示状态切换仅保存在演示进程内存，不写入真实 runtime、不落盘证据、不发送 Matrix 消息</div>
      </div>
    </div>
  );
}

function GateActionsInline({ caseId }) {
  const { mode, next, jumpToReject } = useDemo();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [jumped, setJumped] = useState(false);
  const decide = async (decision) => {
    setBusy(true);
    setJumped(false);
    try {
      const r = await api(`/api/cases/${caseId}/approval?mode=${mode}`, { method: 'POST', body: { decision, actor: 'demo-operator' } });
      setResult(r);
      // Phase 18 demo enhancement: after an accepted replay decision, auto-jump
      // the timeline to the corresponding branch (approve -> next event;
      // reject -> the recorded rejection state). PRESENTATION view only — this
      // takeover renders nowhere else. No runtime/evidence writes.
      if (r && r.accepted === true) {
        if (decision === 'approve') next();
        else if (decision === 'reject') jumpToReject();
        setJumped(true);
      }
    } catch (e) {
      setResult({ accepted: false, error: e.payload?.error, detail: e.payload?.detail, display: 'LIVE APPROVAL UNAVAILABLE — 未显示批准成功' });
    } finally {
      setBusy(false);
    }
  };
  const liveRejected = result && result.accepted === false;
  return (
    <div>
      <div className="takeover-actions">
        <button className="btn primary" disabled={busy} onClick={() => decide('approve')}>批准修复</button>
        <button className="btn danger" disabled={busy} onClick={() => decide('reject')}>拒绝修复</button>
        <button className="btn ghost" disabled={busy} onClick={() => decide('hold')}>保持阻塞</button>
      </div>
      {liveRejected && (
        <div className="takeover-note" style={{ marginTop: 10, borderColor: 'var(--red-deep)', color: 'var(--red)' }}>
          ⨯ LIVE APPROVAL UNAVAILABLE — 审批接口未配置/不可达，不显示批准成功（{result.error}）
        </div>
      )}
      {jumped && (
        <div className="takeover-note" style={{ marginTop: 10 }}>
          ✓ 决定已生效（演示回放）——时间线已跳转到对应分支，可用 下一事件 / 播放 继续走完剩余事件
        </div>
      )}
      {result && result.accepted === true && (
        <pre className="tl-detail" style={{ marginTop: 12 }}>{JSON.stringify(result, null, 2)}</pre>
      )}
    </div>
  );
}
