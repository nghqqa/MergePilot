// frontend/src/components/ui.jsx — shared primitives (split-theater world)
import React from 'react';
import { useNavigate } from 'react-router-dom';
import { useDemo } from '../store.jsx';
import {
  IconPlay, IconPause, IconNext, IconPrev, IconSkipEnd, IconReset,
  IconGate, IconShieldCheck, IconAlert, IconHash,
} from '../icons.jsx';

export function ModeBanner({ modes, mode }) {
  if (!modes) return <span className="mode-banner replay">REPLAY — HISTORICAL VERIFIED RUN</span>;
  if (mode === 'replay') {
    return <span className="mode-banner replay" title="回放模式：从锁定证据目录读取真实历史结果，不写入 runtime">{modes.replay?.banner || 'REPLAY — HISTORICAL VERIFIED RUN'}</span>;
  }
  const live = modes.live;
  if (live?.available) return <span className="mode-banner live" title="Live 模式：连接真实 AgentTeams 数据源">{live.banner}</span>;
  return <span className="mode-banner live-down" title="Live 数据源不可用 — 不用假数据冒充">{live?.unavailable_banner || 'LIVE DATA UNAVAILABLE'}</span>;
}

export function StatusPill({ status, label }) {
  return <span className={`pill ${status}`}>{label ?? status}</span>;
}

export function SourceRef({ children }) {
  if (!children) return null;
  return <div className="src"><IconHash size={12} />{children}</div>;
}

export function LoadingBox({ text = '加载中…' }) {
  return <div className="statebox"><span className="spin">◌</span> {text}</div>;
}

export function ErrorBox({ e, text }) {
  return (
    <div className="statebox error" role="alert">
      <div style={{ display: 'flex', gap: 8, justifyContent: 'center', alignItems: 'baseline' }}>
        <IconAlert /> {text || '数据加载失败'}
      </div>
      <span className="mono">{e?.message} {e?.payload?.detail || ''}</span>
      <div style={{ marginTop: 10 }}>
        <button className="btn ghost" onClick={() => window.location.reload()}>重试</button>
      </div>
    </div>
  );
}

export function EmptyBox({ text }) {
  return <div className="statebox">{text || '暂无数据'}</div>;
}

export function DemoBar() {
  const { activeCase, setActiveCase, idx, next, reset, jumpToGate, jumpToReject, playing, setPlaying, totalEvents, mode, setMode, modes, setIdx, view, setView, currentEvent } = useDemo();
  const navigate = useNavigate();
  const [now, setNow] = React.useState(new Date());
  React.useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);

  const selectCase = (id) => {
    setActiveCase(id);
    navigate(`/cases/${id}`);
  };

  const hasGate = activeCase === 'pr2-high-risk-human-gate' || activeCase === 'pr3-high-risk-human-reject';
  const hasReject = activeCase === 'pr3-high-risk-human-reject';
  const liveAvailable = modes?.live?.available;
  const SRC_LABEL = { matrix: 'Matrix event', minio: 'MinIO task result', evidence: 'Evidence Replay', controller: 'Controller API' };

  return (
    <div className="demobar" role="toolbar" aria-label="演示控制栏">
      <div className="demobar-inner">
        <span className="ctl-label">View</span>
        <div className="viewswitch" role="tablist" aria-label="平台视图">
          {['operations', 'presentation', 'evidence'].map((v) => (
            <button key={v} role="tab" aria-selected={view === v} className={`vtab ${view === v ? 'on' : ''}`} onClick={() => setView(v)}>
              {v.toUpperCase()}
            </button>
          ))}
        </div>
        <span className="ctl-label" style={{ marginLeft: 6 }}>Case</span>
        <select value={activeCase} onChange={(e) => selectCase(e.target.value)} aria-label="选择案例">
          <option value="pr1-normal-review">PR #1 普通协同</option>
          <option value="pr2-high-risk-human-gate">PR #2 高危人工门（批准）</option>
          <option value="pr3-high-risk-human-reject">PR #3 高危人工门（拒绝）</option>
        </select>
        <button className="dbtn" onClick={reset} title="重置回放 (R)"><IconReset />重置</button>
        <button className="dbtn" onClick={() => setPlaying(!playing)} title="开始/暂停回放 (Space)">
          {playing ? <IconPause /> : <IconPlay />}{playing ? '暂停' : '播放'}
        </button>
        <button className="dbtn" onClick={next} disabled={idx >= totalEvents} title="下一事件 (→)"><IconNext />下一事件</button>
        <button className="dbtn" onClick={() => setIdx(totalEvents)} disabled={idx >= totalEvents} title="跳到终态"><IconSkipEnd />终态</button>
        <button className="dbtn" onClick={jumpToGate} disabled={!hasGate} title={hasGate ? '跳转人工门 (G)' : 'PR #1 无人工门（非高危）'}><IconGate />人工门</button>
        <button className="dbtn" onClick={jumpToReject} disabled={!hasReject} title={hasReject ? '跳转人工拒绝（到达后自动停止播放）' : '仅 PR #3 提供拒绝路径'}>跳转拒绝</button>
        <span className="progress">{idx}/{totalEvents}</span>
        {mode === 'replay'
          ? <span className="chip info" title="Replay 模式任何按钮都不写 runtime/Matrix/MinIO/PR">REPLAY ACTION — NO RUNTIME WRITE</span>
          : <span className="chip bad" title="Live 模式审批将调用受保护接口（RUNTIME WRITE）">LIVE ACTION — RUNTIME WRITE</span>}
        <span className="nowplaying" title={currentEvent ? `${currentEvent.source_ref} · ${currentEvent.timestamp} (${currentEvent.timestamp_precision})` : '回放起点'}>
          {currentEvent
            ? <>来源 {SRC_LABEL[currentEvent.source] ?? currentEvent.source} · 精度 {currentEvent.timestamp_precision} · {currentEvent.timestamp}</>
            : <>回放起点 · 数据模式 {mode === 'replay' ? 'Replay' : liveAvailable ? 'Live' : 'Live（不可用）'}</>}
        </span>
        <span className="kbd-hint"><kbd>Space</kbd> 播放 · <kbd>→</kbd> 下一事件 · <kbd>G</kbd> 人工门 · <kbd>R</kbd> 重置</span>
        <button
          className="dbtn"
          onClick={() => setMode(mode === 'replay' ? 'live' : 'replay')}
          title="切换 Replay / Live 模式"
          style={{ marginLeft: 'auto' }}
        >
          <IconShieldCheck />
          {mode === 'replay' ? '切换 Live' : '切换 Replay'}
        </button>
        <span className="faint small mono" title="页面数据更新时间">{now.toLocaleTimeString('zh-CN', { hour12: false })}</span>
      </div>
      {currentEvent?.source_ref && (
        <div className="demobar-ref mono" title={currentEvent.source_ref}>
          EV {String(currentEvent.seq).padStart(2, '0')} ⌗ {currentEvent.source_ref}{currentEvent.matrix_event_id ? ` · matrix ${currentEvent.matrix_event_id}` : ''}
        </div>
      )}
    </div>
  );
}
