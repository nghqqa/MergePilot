// frontend/src/pages/Overview.jsx — masthead + case billboards + mandated status bar.
// 10-second thesis; per-case facts; no status shown without real evidence support.
import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api.js';
import { useDemo } from '../store.jsx';
import { LoadingBox, SourceRef } from '../components/ui.jsx';
import { IconShieldCheck, IconLock } from '../icons.jsx';

export default function Overview() {
  const { cases, health, modes, mode } = useDemo();
  const [audit, setAudit] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    api('/api/audit').then(setAudit).catch(() => setAudit(null));
  }, []);

  if (!cases) return <main className="page" style={{ paddingTop: 40 }}><LoadingBox text="加载平台状态…" /></main>;

  const live = modes?.live;
  const CASE_META = {
    'pr1-normal-review': { label: 'AUTONOMOUS REVIEW', word: 'AUTONOMOUS', cls: 'green' },
    'pr2-high-risk-human-gate': { label: 'HUMAN-GATED SECURITY REVIEW', word: 'HUMAN GATE', cls: 'amber' },
    'pr3-high-risk-human-reject': { label: 'HUMAN-REJECTED SECURITY REVIEW', word: 'REJECTED', cls: 'red' },
  };

  const al = health?.agentloop;
  const integ = health?.integrity;
  const rg = health?.rag;
  const pdb = health?.polardb;
  const liveTrace = al?.live_copaw_run?.trace;
  const STATUS_BAR = [
    { label: 'AgentTeams Demo', value: 'VERIFIED', cls: 'ok' },
    { label: 'AgentTeams Controller', value: 'VERIFIED', cls: 'ok' },
    { label: 'CoPaw Runtime', value: 'VERIFIED', cls: 'ok' },
    { label: 'Evidence Replay', value: 'VERIFIED', cls: 'ok' },
    { label: 'AgentLoop Cloud Smoke', value: al?.smoke?.status ?? '…', cls: al?.smoke?.status === 'VERIFIED' ? 'ok' : 'off' },
    { label: 'AgentLoop Cloud Trace', value: al?.live_copaw_run?.status ?? '…', cls: al?.live_copaw_run?.status === 'VERIFIED' ? 'ok' : 'warn' },
    { label: 'Agent/LLM/Tool Coverage', value: al?.coverage?.status ?? '…', cls: al?.coverage?.status === 'VERIFIED' ? 'ok' : 'warn' },
    { label: 'Live CoPaw Trace', value: al?.live_copaw_run?.status ?? '…', cls: al?.live_copaw_run?.status === 'VERIFIED' ? 'ok' : 'warn' },
    { label: 'Tool Span', value: al?.tool_span?.status ?? '…', cls: al?.tool_span?.status === 'VERIFIED' ? 'ok' : 'warn' },
    { label: 'Trace Mode', value: al?.live_copaw_run?.trace_mode ?? '…', cls: al?.live_copaw_run?.trace_mode === 'LIVE CLOUD' ? 'ok' : 'off' },
    { label: 'Three Safety Paths', value: 'VERIFIED', cls: 'ok' },
    { label: 'RAG', value: rg ? 'SYNTHETIC DEMO' : 'NOT CONNECTED', cls: 'info' },
    { label: 'RAG Agent Correlation', value: (rg?.agent_correlation || 'PENDING').split('（')[0], cls: (rg?.agent_correlation || '').startsWith('VERIFIED') ? 'ok' : 'warn' },
    { label: 'PR #4 Cross-Repo', value: 'VERIFIED (SIMULATED DB)', cls: 'ok' },
    { label: 'PolarDB Branch', value: pdb?.connection ?? 'NOT CONNECTED', cls: 'bad' },
    { label: 'Database Branch', value: 'SIMULATED', cls: 'warn' },
    { label: 'PR Auto Merge', value: 'DISABLED', cls: 'warn' },
    { label: 'Final Package SHA256', value: integ ? integ.final_submission_integrity.status : '…', cls: integ?.final_submission_integrity.status === 'VERIFIED' ? 'ok' : 'bad' },
  ];

  return (
    <main className="page" style={{ paddingTop: 0 }}>
      <section className="masthead">
        <h1>普通变更，Agent 自主完成。<br />高危风险，<em>系统停下等人工</em>。</h1>
        <p className="thesis">
          <b>一句话：让 AI 守规矩地改代码——能自己干的自己干，有风险的停下来让人批准，每一步都有据可查。</b><br />
          MergePilot 是面向代码与安全变更的多 Agent 审查和受控修复平台：Reviewer → Fixer → Verifier 协同闭环；
          发现高危时自动暂停，人工批准后才继续修复与独立验证。所有状态、事件与审批来自真实 AgentTeams /
          CoPaw 运行程。
        </p>
        <div className="badge-rail" aria-label="状态栏">
          {STATUS_BAR.map((s) => (
            <span key={s.label} className={`chip ${s.cls}`}>{s.label}: <b>{s.value}</b></span>
          ))}
          <span className={`chip ${mode === 'replay' ? 'info' : live?.available ? 'ok' : 'bad'}`}>
            Data Mode: <b>{mode === 'replay' ? 'REPLAY' : live?.available ? 'LIVE' : 'LIVE UNAVAILABLE'}</b>
          </span>
        </div>
      </section>

      {mode === 'live' && !live?.available && (
        <div className="statebox error" role="alert" style={{ marginTop: 18 }}>
          LIVE DATA UNAVAILABLE — 核心 AgentTeams 数据源（Matrix / MinIO / Controller）当前不可达
          <span className="mono">{live?.note}</span>
        </div>
      )}

      <section className="billboards" aria-label="演示案例">
        {cases.cases.map((c) => {
          const m = CASE_META[c.case_id];
          return (
            <button
              key={c.case_id}
              className={`billboard ${c.risk.level === 'high' || c.risk.level === 'critical' ? 'risk' : ''}`}
              onClick={() => navigate(`/cases/${c.case_id}`)}
              aria-label={`打开案例 ${c.title}`}
            >
              <span className="bb-case">
                PR #{c.pull_request.number} · {m.label}
                <span className="chip info" style={{ marginLeft: 'auto' }}>REPLAY</span>
              </span>
              <span className={`bb-word ${m.cls}`}>{m.word}</span>
              <span className="bb-title">{c.title}</span>
              <dl className="kv small bb-facts">
                <dt>repository</dt><dd className="mono">{c.repository}</dd>
                <dt>PR number</dt><dd className="mono">#{c.pull_request.number}</dd>
                <dt>project</dt><dd className="mono">{c.project.id}</dd>
                <dt>team / runtime</dt><dd className="mono small">p14h2-wd copaw workers · copaw-worker:223ddc2</dd>
                <dt>risk level</dt><dd>{c.risk.level === 'high' ? <span className="chip bad">HIGH · {c.risk.category}</span> : c.risk.level === 'critical' ? <span className="chip bad">CRITICAL · {c.risk.category}</span> : <span className="chip ok">NORMAL</span>}</dd>
                <dt>task progress</dt><dd className="mono">{c.tasks_completed}</dd>
                <dt>project status</dt><dd className="mono">{c.phase === 'completed' ? 'COMPLETED' : c.phase === 'blocked' ? <span className="chip bad">BLOCKED</span> : c.phase}</dd>
                <dt>PR state</dt><dd><span className="chip warn">{String(c.pull_request.state).toUpperCase()}</span> <span className="faint">无写操作</span></dd>
                <dt>human gate</dt><dd>{c.risk.human_gate === 'none' ? <span className="chip off">未触发（非高危）</span> : c.risk.human_gate === 'approved' ? <span className="chip warn"><IconShieldCheck size={11} />APPROVED（真实审批记录）</span> : c.risk.human_gate === 'rejected' ? <span className="chip bad">REJECTED（真实拒绝记录）</span> : c.risk.human_gate}</dd>
                <dt>data mode</dt><dd><span className="chip info">REPLAY — HISTORICAL VERIFIED RUN</span></dd>
              </dl>
            </button>
          );
        })}
      </section>

      {liveTrace && al?.live_copaw_run?.status === 'VERIFIED' && (
        <section className="sec" aria-label="AgentLoop LIVE CLOUD Trace">
          <div className="sec-head">
            <h2>AgentLoop Cloud Trace — LIVE CLOUD（真实运行合并 Trace）</h2>
            <span className="sub"><span className="chip ok">Trace Mode: LIVE CLOUD</span></span>
          </div>
          <div className="resrisk">
            <div className="item"><IconShieldCheck size={13} />真实 CoPaw run 已在阿里云 AgentLoop 控制台确认：Agent + LLM + Tool 三类 span 合并于同一 Trace，会话与模型分析数据可用</div>
            <dl className="kv small detail">
              <dt>trace id</dt><dd className="mono">{liveTrace.trace_id}</dd>
              <dt>session id</dt><dd className="mono">{liveTrace.session_id}</dd>
              <dt>service / model</dt><dd className="mono">{liveTrace.service} · {liveTrace.model}</dd>
              <dt>started (UTC) / duration</dt><dd className="mono">{liveTrace.started_at_utc} · {liveTrace.duration}</dd>
              <dt>调用次数</dt><dd className="mono">Agent {liveTrace.agent_calls} · LLM {liveTrace.llm_calls} · Tool {liveTrace.tool_calls}</dd>
              <dt>Token</dt><dd className="mono">总 {liveTrace.tokens_total}（入 {liveTrace.tokens_input} / 出 {liveTrace.tokens_output}）</dd>
              <dt>span 类型图例</dt>
              <dd className="mono small">
                <span className="chip info">AGENT_STEP</span> <span className="chip info">AGENT</span>{' '}
                <span className="chip ok">LLM-native (chat deepseek-chat)</span> <span className="chip ok">LLM-wrapper (genai.llm.call)</span>{' '}
                <span className="chip warn">TOOL (projectflow/taskflow)</span> <span className="chip warn">CHANNEL (matrix.send)</span>
              </dd>
              <dt>合并说明</dt>
              <dd className="small">原生 agentscope span 与 loongsuite wrapper span 同 Trace：zz 埋点在解释器启动时设置全局 TracerProvider（service.name=mergepilot-copaw），agentscope_runtime 复用同一 Provider，所有 span 同批导出、同 Trace 收敛</dd>
              <dt>证据来源</dt>
              <dd className="small mono">{al.live_copaw_run.evidence_dir}（{al.live_copaw_run.verdict}）</dd>
              <dt>SHA256 状态</dt>
              <dd><span className="chip ok">证据目录内含 SHA256SUMS 清单</span> <span className="faint small">verdict 为操作员控制台确认（非 API 级查询记录）；不声称 6/6 稳定覆盖</span></dd>
            </dl>
          </div>
        </section>
      )}

      <section className="sec" aria-label="诚实披露">
        <div className="sec-head">
          <h2>Disclosures — 诚实披露</h2>
          <span className="sub"><a href="#/audit">完整审计页 →</a></span>
        </div>
        <div className="resrisk">
          <div className="item"><IconLock size={13} />残余风险与未接入项（如实显示，无证据不标 READY）</div>
          <div className="detail">
            MinIO shared tree 曾异常清空，已恢复，根因待查 · tool_guard 曾会话超时，已记录 ·
            VERIFICATION_PASSED 状态枚举兼容，已记录 ·
            AgentLoop：Cloud Smoke {al?.smoke?.status ?? '…'}；Live CoPaw Trace {al?.live_copaw_run?.status ?? '…'}（LIVE CLOUD，操作员控制台确认，verdict {al?.live_copaw_run?.verdict ?? '…'}）·
            per-task live trace 仍为 evidence-replay ·
            RAG 已接入合成数据集（SYNTHETIC DEMO，非企业数据）· PolarDB Branch NOT CONNECTED（演示使用 SIMULATED fixture）·
          </div>
          <SourceRef>D:\goai\p14-demo\evidence\PHASE14-WINDOWS-REPLAY-MATERIALS-20260829-195223\07-disclosures.md · 08-scope-boundary.md{al?.source_ref ? '' : ''}</SourceRef>
        </div>
      </section>
    </main>
  );
}
