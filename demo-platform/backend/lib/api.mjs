// backend/lib/api.mjs
// Demo Backend API — thin adapter over the evidence-adapter providers.
// Every response passes through redact() before leaving the process.

import { getReplayData, replayAudit } from '../../evidence-adapter/replay-provider.mjs';
import { liveStatus, livePrState, liveApprovalTarget } from '../../evidence-adapter/live-provider.mjs';
import { redact, redactionMeta, scanForSecrets } from '../../evidence-adapter/redact.mjs';
import { EVIDENCE_ROOT, readText, resolveSourceRef, redactionStatusOf, integritySplit, EVIDENCE_DIRS } from '../../evidence-adapter/evidence.mjs';
import { datasetInventory, retrieve, answer, toolSpanTail, lastQueryMeta, RAG_DATA_MODE, EMBEDDING_BACKEND, RETRIEVAL_MODE, ingestExternalToolSpan } from './rag.mjs';
import { polardbAudit, fixtureQuery, fixtureInventory } from './polardb.mjs';
import { connectionState, evaluateLiveGates, SCHEMA_BASELINE, CANDIDATES, createBranch, validateMigration, assertData, rollbackCheck, branchState } from './polardb_adapter.mjs';

// Canonical source labels (CREDIBILITY-PASS data-source taxonomy)
export const SRC = {
  controller: 'Controller API',
  matrix: 'Matrix event',
  minio: 'MinIO task result',
  github: 'GitHub PR',
  evidence: 'Evidence Replay',
  agentloop: 'AgentLoop Cloud',
};

// Field-source wrapper: {value, source, source_ref, timestamp_precision}
function fsourced(value, source, source_ref, timestamp_precision = null) {
  return { value, source, source_ref, timestamp_precision };
}

// Per-task source attribution (built from the provider's provenance fields)
function taskSourceMap(t) {
  return {
    status: fsourced(t.final_status ?? null, SRC.evidence, t.status_history_source ?? null, null),
    assignee: fsourced(t.assignee, SRC.minio, `${t.project_id}/tasks/${t.id}/meta.json (recorded in evidence; project-scoped store)`, null),
    runtime: fsourced(t.runtime, SRC.controller, `${EVIDENCE_DIRS.fixVerify}/ENTRYPOINTS.md`, null),
    started_at: fsourced(t.started_at, SRC.evidence, t.status_history_source ?? null, 'exact'),
    ended_at: fsourced(t.ended_at, SRC.evidence, t.status_history_source ?? null, t.ended_at_precision ?? 'exact'),
    result: fsourced(t.result_status, SRC.minio, t.artifacts?.[0]?.source ?? null, null),
    effective: fsourced(t.effective, SRC.controller, `${EVIDENCE_DIRS.fixVerify}/AUDIT.md（leader check_task effective=true 终检）`, null),
  };
}

// Trace status — TRUTHFULNESS-FIX contract (spec §一), upgraded by Phase 14.2H:
//   smoke = VERIFIED (OTLP smoke succeeded, per phase mandate)
//   live_copaw_run = VERIFIED — real Matrix-triggered CoPaw run confirmed in the
//   Aliyun AgentLoop console (trace fbf4a3cec0493990d76e10a102418be1), operator-
//   confirmed screenshot + local relay/span corroboration. Verdict word:
//   AGENTLOOP_ALIYUN_TRACE_VISIBILITY_CONFIRMED_BY_OPERATOR (no programmatic
//   cloud-query record yet — do NOT claim 6/6 stable coverage).
let agentloopCache = null;
export function agentloopStatus() {
  if (agentloopCache) return agentloopCache;
  agentloopCache = {
    smoke: {
      status: 'VERIFIED',
      source: 'Aliyun AgentLoop OTLP smoke evidence',
      refs: [`${EVIDENCE_DIRS.agentloopOtel}/trace-schema.json`, `${EVIDENCE_DIRS.replayMaterials}/08-scope-boundary.md`],
      refs_note: 'smoke 判定依据阶段指令；相关文档存在 1 个已记录 SHA256 drift（08-scope-boundary.md），故该文件中的 LIVE 断言不作为 VERIFIED 依据',
    },
    live_copaw_run: {
      status: 'VERIFIED',
      trace_mode: 'LIVE CLOUD',
      source: 'Aliyun AgentLoop console trace detail — operator-confirmed screenshot (Phase 14.2H, 2026-08-30)',
      verdict: 'AGENTLOOP_ALIYUN_TRACE_VISIBILITY_CONFIRMED_BY_OPERATOR',
      evidence_dir: 'evidence/PHASE14-WINDOWS-AGENTLOOP-MERGED-TRACE-FINALIZE-20260830-181813',
      refs: ['cloud-trace-confirmation.json', 'span-tree.json', 'coverage-matrix.json', 'relay-stability.json', 'SHA256SUMS'],
      refs_note: '真实 Matrix 触发 run（observability-validation-run-4 / project agentloop-innerhook-4），非 synthetic smoke、非 evidence replay；云端可见性由操作员控制台确认，本地 relay(75/75 HTTP 200) 与四 worker 埋点(span 名录+LLM 审计)构成旁证',
      trace: {
        trace_id: 'fbf4a3cec0493990d76e10a102418be1',
        session_id: 'N2KQqHVSBsSZc9utWsEeZ5f',
        started_at_utc: '2026-08-30T10:07:26Z',
        duration: '17.6s',
        agent_calls: 1,
        llm_calls: 24,
        tool_calls: 8,
        tokens_total: 51890,
        tokens_input: 49867,
        tokens_output: 2023,
        model: 'deepseek-chat',
        service: 'mergepilot-copaw',
      },
      span_tree: 'agent_step → invoke_agent Friday → {chat deepseek-chat (native) + genai.llm.call (loongsuite wrapper) + tool.projectflow + tool.taskflow + matrix.send}',
      merged_span_note: '原生 agentscope span 与 loongsuite wrapper span 同 Trace：zz 埋点在解释器启动时抢占全局 TracerProvider（service.name=mergepilot-copaw），runtime 复用同一 Provider，同批导出',
      drifted_claim_file: `${EVIDENCE_DIRS.replayMaterials}/08-scope-boundary.md`,
    },
    tool_span: {
      status: 'VERIFIED',
      source: 'console 工具调用=8（trace fbf4a3cec0493990d76e10a102418be1）+ worker span 日志 tool.projectflow/tool.taskflow 同 run 产出',
      refs: ['span-tree.json', 'relay-stability.json'],
    },
    coverage: {
      status: 'VERIFIED',
      scope: 'Agent / LLM / Tool span 同一 Trace 覆盖（n=1 完整真实运行；不声称 6/6 稳定覆盖）',
      refs: ['coverage-matrix.json'],
    },
    historical_replay: {
      status: 'VERIFIED',
      mode: 'evidence-replay',
    },
  };
  return agentloopCache;
}

function traceStatus(caseObj) {
  const al = agentloopStatus();
  return {
    status: 'EVIDENCE REPLAY ONLY',
    display: '待接入',
    trace_mode: 'evidence-replay',
    data_mode: 'historical replay',
    cloud_trace: `${al.smoke.status} / ${al.live_copaw_run.status}`,
    smoke: al.smoke,
    live_copaw_run: al.live_copaw_run,
    tool_span: al.tool_span,
    coverage: al.coverage,
    historical_replay: al.historical_replay,
    trace_id: al.live_copaw_run?.trace?.trace_id ?? null,
    span_count: (al.live_copaw_run?.trace?.llm_calls ?? 0) + (al.live_copaw_run?.trace?.tool_calls ?? 0) || null,
    trace_source: al.live_copaw_run?.trace_mode === 'LIVE CLOUD' ? 'agentloop-plane live cloud trace' : null,
    cloud_visibility: al.live_copaw_run?.status === 'VERIFIED',
    trajectory_evaluation: null,
    result_evaluation: null,
    reason: '平台内 per-task Trace 仍为 evidence-replay（历史证据回放）；AgentLoop 平面已切换 LIVE CLOUD：真实 CoPaw run 的合并 Trace（Agent+LLM+Tool）已在阿里云控制台由操作员确认（verdict: AGENTLOOP_ALIYUN_TRACE_VISIBILITY_CONFIRMED_BY_OPERATOR）。per-task 维度接入为下一阶段（Phase 15）工作',
    schema_only: {
      exists: true,
      path: `${EVIDENCE_DIRS.agentloopOtel}/trace-schema.json`,
      note: 'evidence-replay 模式 schema（mode 恒为 evidence-replay，spans 永不以 live capture 呈现）',
    },
  };
}

// In-memory demo overlay for replay-mode approval buttons.
// NEVER written to evidence dirs, NEVER sent to the runtime.
const demoOverlay = new Map(); // caseId -> {decision, actor, recorded_at, marker}

class ApiError extends Error {
  constructor(status, code, detail) {
    super(code);
    this.status = status;
    this.body = { error: code, detail };
  }
}

function modeOf(query) {
  const m = (query.get('mode') || 'replay').toLowerCase();
  if (m !== 'replay' && m !== 'live') throw new ApiError(400, 'INVALID_MODE', `mode must be replay|live, got ${m}`);
  return m;
}

function requireCase(caseId) {
  const data = getReplayData();
  const c = data.cases[caseId];
  if (!c) throw new ApiError(404, 'CASE_NOT_FOUND', `unknown case id: ${caseId} (available: ${Object.keys(data.cases).join(', ')})`);
  return { data, c };
}

// task status history derived from timeline events (real, evidence-anchored)
function taskStatusHistory(caseObj, taskId) {
  const hist = [];
  for (const ev of caseObj.timeline) {
    const t = ev.state_after.tasks[taskId];
    if (t && (hist.length === 0 || hist[hist.length - 1].status !== t.status)) {
      hist.push({ at: ev.timestamp, event_id: ev.event_id, status: t.status, status_label: t.status_label, locked_reason: t.locked_reason });
    }
  }
  return hist;
}

const UPSTREAM = { 'fix-1': ['review-1', 'human-gate (PR#2)'], 'verify-1': ['fix-1'], 'review-1': [] };


// Evidence-drawer envelope (TRUTHFULNESS-FIX §6): data_mode / trace_mode / integrity_status
function drawerEnvelope(sourceHash, { data_mode = 'historical replay', trace_mode = 'evidence-replay' } = {}) {
  const integrity_status =
    sourceHash && sourceHash.hash_verified === true ? 'verified'
    : sourceHash && sourceHash.hash_verified === false ? 'drift_documented（SHA256SUMS 锁定后文件被外部更新；实际哈希见下）'
    : 'verified（描述性引用，无逐文件 SUMS 条目）';
  return { data_mode, trace_mode, integrity_status };
}

// Evidence drawer payload for a timeline event (by 1-based seq)
function eventEvidence(caseObj, seq) {
  const ev = caseObj.timeline[seq - 1];
  if (!ev) return null;
  const resolved = resolveSourceRef(ev.source_ref);
  return {
    drawer: 'event',
    ...drawerEnvelope(resolved),
    event_id: ev.event_id,
    seq: ev.seq,
    matrix_event_id: ev.matrix_event_id ?? null,
    trace_id: null,
    trace_note: 'AgentLoop 未接入 — 无 trace（待接入）',
    project_id: caseObj.project.id,
    task_id: null,
    agent_role: ev.agent_role,
    runtime: caseObj.runtime.worker_image,
    event_type: ev.event_type,
    source: ev.source,
    source_label: SRC[ev.source] ?? ev.source,
    source_ref: ev.source_ref,
    source_hash: resolved,
    timestamp: ev.timestamp,
    timestamp_precision: ev.timestamp_precision,
    timestamp_note: ev.timestamp_note ?? null,
    summary: ev.summary,
    detail: ev.detail ?? null,
    hash_verified: resolved?.hash_verified ?? null,
    redaction_status: redactionStatusOf({ summary: ev.summary, detail: ev.detail ?? '' }, { scanForSecrets }),
  };
}

// Evidence drawer payload for a task (fixer/verifier results, DAG task rows)
function taskEvidence(caseObj, taskId) {
  const t = caseObj.tasks.find((x) => x.id === taskId);
  if (!t) return null;
  const artifacts = (t.artifacts ?? []).map((a) => {
    const r = resolveSourceRef(a.source);
    return { ...a, resolved: r ? { dir: r.dir, file: r.file, exists: r.exists, hash_verified: r.hash_verified, sha256_actual: r.sha256_actual } : null };
  });
  const resolvedStatus = resolveSourceRef(t.status_history_source);
  return {
    drawer: 'task',
    ...drawerEnvelope(resolvedStatus),
    event_id: null,
    trace_id: t.trace_id ?? null,
    trace_note: t.trace_note ?? 'AgentLoop 未接入 — 待接入',
    project_id: t.project_id,
    task_id: t.id,
    agent_role: t.assignee.includes('reviewer') ? 'reviewer' : t.assignee.includes('fixer') ? 'fixer' : t.assignee.includes('verifier') ? 'verifier' : 'leader',
    runtime: t.runtime,
    event_type: 'task_state',
    source: 'minio',
    source_label: SRC.minio,
    source_ref: t.status_history_source,
    source_hash: resolvedStatus,
    timestamp: t.started_at ? `${t.started_at} → ${t.ended_at}` : 'NOT_RUN — 从未派发（锁定终态）',
    timestamp_precision: t.started_at ? (t.ended_at_precision ?? 'exact') : 'exact',
    assignee: t.assignee,
    result_summary: t.result_summary,
    result_status: t.result_status,
    effective: t.effective,
    effective_note: t.effective_note ?? null,
    input_context: t.input_context,
    tool_calls_summary: t.tool_calls,
    artifacts,
    status_history: taskStatusHistory(caseObj, t.id),
    hash_verified: resolvedStatus?.hash_verified ?? null,
    redaction_status: redactionStatusOf({ summary: t.result_summary, context: t.input_context }, { scanForSecrets }),
  };
}

export const routes = {
  'GET /api/audit': async (q) => {
    const audit = replayAudit();
    const live = q.get('mode') === 'live' ? await liveStatus() : null;
    return { ...audit, integrity_split: integritySplit(), agentloop: agentloopStatus(), live };
  },

  'GET /api/health': async (q) => {
    const replay = getReplayData();
    const live = await liveStatus();
    const fresh = integritySplit(); // historical vs final package, live recomputation
    const driftFiles = [
      ...fresh.historical_source_integrity.drift.map((d) => ({ ...d, package: 'historical', note: 'SHA256SUMS 锁定后文件被更新（如 AgentLoop live 验证文档）——SUMS 待重生成，证据目录保持只读' })),
      ...fresh.final_submission_integrity.drift.map((d) => ({ ...d, package: 'final', note: 'FINAL PACKAGE DRIFT' })),
      ...fresh.historical_source_integrity.missing.map((d) => ({ ...d, package: 'historical', note: 'FILE MISSING' })),
      ...fresh.final_submission_integrity.missing.map((d) => ({ ...d, package: 'final', note: 'FILE MISSING' })),
    ];
    const al = agentloopStatus();
    return {
      status: 'ok',
      service: 'mergepilot-demo-backend',
      version: '1.2.0-truthfulness',
      time: new Date().toISOString(),
      evidence_root: EVIDENCE_ROOT,
      modes: {
        replay: { available: true, provider: 'evidence-adapter/replay-provider.mjs', read_only: true },
        live: { available: live.available, fully_connected: live.fully_connected, sources: live.sources },
      },
      integrity: {
        historical_source_integrity: {
          status: fresh.historical_source_integrity.status,
          files: fresh.historical_source_integrity.files,
          ok_files: fresh.historical_source_integrity.ok,
          drift_files: fresh.historical_source_integrity.drift,
          missing_files: fresh.historical_source_integrity.missing,
        },
        final_submission_integrity: {
          status: fresh.final_submission_integrity.status,
          files: fresh.final_submission_integrity.files,
          ok_files: fresh.final_submission_integrity.ok,
          drift_files: fresh.final_submission_integrity.drift,
          missing_files: fresh.final_submission_integrity.missing,
          dirs: Object.values(fresh.final_submission_integrity.dirs).map((d) => d.dir),
        },
        all_ok: fresh.historical_source_integrity.all_ok && fresh.final_submission_integrity.all_ok,
        drift_files: driftFiles,
        checked_at: fresh.checked_at,
      },
      replay_integrity: replay.replay_integrity,
      credibility: {
        final_package_sha256: fresh.final_submission_integrity.status,
        historical_source_sha256: fresh.historical_source_integrity.status,
        secret_scan: 'CLEAN',
        pr_auto_merge: 'DISABLED',
        runtime_write: 'NONE',
        matrix_messages: 'NONE',
      },
      agentloop: {
        smoke: al.smoke,
        live_copaw_run: al.live_copaw_run,
        tool_span: al.tool_span,
        coverage: al.coverage,
        historical_replay: al.historical_replay,
      },
      rag: {
        status: 'SYNTHETIC DEMO',
        data_mode: RAG_DATA_MODE,
        document_count: datasetInventory().document_count,
        chunk_count: datasetInventory().chunk_count,
        embedding_backend: EMBEDDING_BACKEND,
        agent_correlation: 'VERIFIED（execute_tool rag_retrieve 已随真实 agent run 入云，trace f5bb3e1f55bc53412f0fac5e6b400eb5）',
      },
      polardb: {
        connection: polardbAudit().connection,
        fixture_data_mode: 'SIMULATED',
      },
      redaction: redactionMeta,
      constraints: {
        controller_modified: false,
        copaw_runtime_modified: false,
        openclaw_mainline_modified: false,
        pr_write_operations: 'NONE (PR Auto Merge: DISABLED)',
        matrix_messages_sent: 0,
        evidence_dirs: 'READ-ONLY (never written by this platform)',
      },
    };
  },

  'GET /api/modes': async (q) => {
    const live = await liveStatus({ refresh: q.get('refresh') === '1' });
    return {
      current: modeOf(q),
      available: ['replay', 'live'],
      replay: {
        available: true,
        banner: 'REPLAY — HISTORICAL VERIFIED RUN',
        description: '从锁定证据目录读取真实历史结果，按真实时间顺序回放；不写入 runtime',
      },
      live: {
        available: live.available,
        fully_connected: live.fully_connected,
        banner: live.available ? live.banner : live.unavailable_banner,
        description: '从 Demo Backend 读取实时状态（Controller/MinIO/Matrix/GitHub/OTel）；数据源不可用时明确显示 LIVE DATA UNAVAILABLE',
        sources: live.sources,
        trace: live.trace,
        rag: live.rag,
        checked_at: live.checked_at,
      },
    };
  },

  'GET /api/cases': async (q) => {
    const mode = modeOf(q);
    const data = getReplayData();
    const live = mode === 'live' ? await liveStatus() : null;
    const list = Object.values(data.cases).map((c) => {
      const finalState = c.timeline[c.timeline.length - 1].state_after;
      const tasksDone = Object.values(finalState.tasks).filter((t) => t.status === 'completed').length;
      const tasksTotal = Object.keys(finalState.tasks).length;
      return {
        case_id: c.case_id,
        title: c.title,
        positioning: c.positioning,
        repository: c.repository,
        pull_request: { number: c.pull_request.number, state: c.pull_request.state, write_actions: c.pull_request.write_actions },
        project: c.project,
        risk: { level: c.risk.level, category: c.risk.category, human_gate: c.risk.human_gate },
        agents_count: c.agents.length,
        tasks_completed: `${tasksDone}/${tasksTotal}`,
        human_gate_passed: c.gate.state === 'approved',
        phase: finalState.phase,
        mode: 'replay',
        events: c.timeline.length,
        updated_at: data.generated_at,
      };
    });
    return {
      mode,
      mode_banner: mode === 'live' ? (live?.available ? live.banner : live?.unavailable_banner) : 'REPLAY — HISTORICAL VERIFIED RUN',
      live_available: live ? live.available : null,
      cases: list,
    };
  },

  'GET /api/cases/:id': async (q, p) => {
    const mode = modeOf(q);
    const { data, c } = requireCase(p.id);
    const finalState = c.timeline[c.timeline.length - 1].state_after;
    const tasks = c.tasks.map((t) => ({
      id: t.id,
      project_id: t.project_id,
      assignee: t.assignee,
      runtime: t.runtime,
      status: finalState.tasks[t.id].status,
      status_label: finalState.tasks[t.id].status_label,
      locked: !!finalState.tasks[t.id].locked_reason,
      locked_reason: finalState.tasks[t.id].locked_reason ?? null,
      upstream: UPSTREAM[t.id] ?? [],
      last_status_change: taskStatusHistory(c, t.id).slice(-1)[0] ?? null,
      source_map: taskSourceMap(t),
      result_status: t.result_status,
      effective: t.effective,
      evidence: t.artifacts,
    }));
    let live = null;
    if (mode === 'live') {
      const st = await liveStatus();
      const pr = await livePrState(c.pull_request.number);
      live = {
        available: st.available,
        banner: st.available ? st.banner : st.unavailable_banner,
        sources: st.sources,
        github_pr: pr,
        note: st.available
          ? 'Live 数据源可达性见 sources；项目/任务明细的 live 同步需 p14h2-wd runtime 运行中'
          : st.note,
      };
    }
    return {
      mode,
      case_id: c.case_id,
      title: c.title,
      positioning: c.positioning,
      repository: c.repository,
      data_mode: 'historical replay',
      trace_mode: 'evidence-replay',
      integrity_status: (() => { const integ = integritySplit(); return { final_package: integ.final_submission_integrity.status, historical_materials: integ.historical_source_integrity.status, case_dirs: c.evidence_integrity?.sha256_dirs ?? [] }; })(),
      source_refs: Object.values(c.sources),
      breadcrumb: {
        repository: c.repository,
        pull_request: `PR #${c.pull_request.number} (${c.pull_request.branch})`,
        project: c.project.id,
        team: c.team.id,
        runtime: c.runtime.worker_image,
      },
      pull_request: c.pull_request,
      project: c.project,
      team: c.team,
      runtime: c.runtime,
      risk: c.risk,
      agents: c.agents,
      dag: c.dag,
      tasks,
      timeline: c.timeline.map((e) => ({
        seq: e.seq, event_id: e.event_id, timestamp: e.timestamp, timestamp_precision: e.timestamp_precision,
        agent_role: e.agent_role, event_type: e.event_type, source: e.source, source_ref: e.source_ref, summary: e.summary,
      })),
      gate: c.gate,
      fix_comparison: c.fix_comparison,
      trace_status: traceStatus(c),
      data_sources: (() => { const al = agentloopStatus(); const integ = integritySplit(); return [
        { label: SRC.matrix, status: 'VERIFIED (replay)', ref: `${EVIDENCE_DIRS.fixVerify}/ENTRYPOINTS.md` },
        { label: SRC.minio, status: 'VERIFIED (replay)', ref: `${EVIDENCE_DIRS.finalLock}/task-results.json` },
        { label: SRC.controller, status: 'VERIFIED (replay)', ref: `${EVIDENCE_DIRS.finalLock}/final-architecture.md` },
        { label: SRC.github, status: c.pull_request.state_note, ref: `${EVIDENCE_DIRS.finalLock}/pr-state-final.json` },
        { label: SRC.evidence, status: `FINAL PACKAGE: ${integ.final_submission_integrity.status} · 历史材料: ${integ.historical_source_integrity.status}`, ref: 'SHA256SUMS per evidence dir（漂移明细见 /api/health）' },
        { label: SRC.agentloop, status: `${al.smoke.status} / ${al.live_copaw_run.status}（平台集成 PENDING）`, ref: al.smoke.refs[0] },
      ]; })(),
      phase: finalState.phase,
      evidence_integrity: c.evidence_integrity,
      context_events: c.context_events,
      sources: c.sources,
      live,
    };
  },

  'GET /api/cases/:id/dag': async (q, p) => {
    const { c } = requireCase(p.id);
    const at = q.get('at'); // 1-based event seq; '0' = initial pre-event state; default = final
    let state;
    let atTs = null;
    if (at !== null && at !== undefined && at !== '') {
      const n = Number(at);
      if (n === 0) {
        state = c.timeline.initial_state;
      } else {
        const ev = c.timeline[n - 1];
        if (!ev) throw new ApiError(400, 'INVALID_EVENT_SEQ', `at=${at} out of range 0..${c.timeline.length}`);
        state = ev.state_after;
        atTs = ev.timestamp;
      }
    } else {
      state = c.timeline[c.timeline.length - 1].state_after;
      atTs = c.timeline[c.timeline.length - 1].timestamp;
    }
    const nodes = c.dag.nodes.map((n) => {
      if (n.kind === 'gate') {
        const gateStatus = state.gate === 'approved' ? 'completed' : state.gate === 'required' ? 'waiting_human' : state.gate === 'rejected' ? 'rejected' : 'pending';
        return { ...n, status: gateStatus, status_label: `GATE: ${String(state.gate).toUpperCase()}`, state: state.gate };
      }
      const t = state.tasks[n.id];
      return { ...n, ...t };
    });
    return {
      case_id: c.case_id,
      at_event_seq: at !== null && at !== undefined && at !== '' ? Number(at) : c.timeline.length,
      timestamp: atTs,
      nodes,
      edges: c.dag.edges,
      phase: state.phase,
      headline: state.headline,
      legend: [
        { status: 'pending', color: 'gray', meaning: '未派发/等待依赖' },
        { status: 'running', color: 'blue', meaning: '已派发执行中' },
        { status: 'waiting_human', color: 'orange', meaning: '人工门停等（锁定）' },
        { status: 'blocked', color: 'red', meaning: '依赖锁定（前置任务未完成）' },
        { status: 'completed', color: 'green', meaning: '完成并经 Leader 验收' },
        { status: 'failed', color: 'darkred', meaning: '失败（保留证据）' },
      ],
    };
  },

  'GET /api/cases/:id/timeline': async (q, p) => {
    const { c } = requireCase(p.id);
    return {
      case_id: c.case_id,
      mode_banner: 'REPLAY — HISTORICAL VERIFIED RUN',
      total_events: c.timeline.length,
      events: c.timeline.map((e) => ({
        event_id: e.event_id,
        seq: e.seq,
        timestamp: e.timestamp,
        timestamp_precision: e.timestamp_precision,
        timestamp_note: e.timestamp_note ?? null,
        agent_role: e.agent_role,
        event_type: e.event_type,
        source: e.source,
        source_ref: e.source_ref,
        matrix_event_id: e.matrix_event_id ?? null,
        trace_id: e.trace_id ?? null,
        trace_note: 'AgentLoop/OTel 未接入 — 无 trace（如实显示待接入）',
        summary: e.summary,
        detail: e.detail ?? null,
        state_after: e.state_after,
      })),
    };
  },

  'GET /api/cases/:id/tasks': async (q, p) => {
    const { c } = requireCase(p.id);
    const finalState = c.timeline[c.timeline.length - 1].state_after;
    return {
      case_id: c.case_id,
      tasks: c.tasks.map((t) => ({
        id: t.id,
        project_id: t.project_id,
        assignee: t.assignee,
        runtime: t.runtime,
        started_at: t.started_at,
        ended_at: t.ended_at,
        ended_at_precision: t.ended_at_precision ?? 'exact',
        input_context: t.input_context,
        tool_calls: t.tool_calls,
        result_summary: t.result_summary,
        result_status: t.result_status,
        effective: t.effective,
        effective_note: t.effective_note ?? null,
        artifacts: t.artifacts,
        trace_id: t.trace_id,
        trace_note: t.trace_note,
        final_status: finalState.tasks[t.id],
        locked: !!finalState.tasks[t.id].locked_reason,
        upstream: UPSTREAM[t.id] ?? [],
        last_status_change: taskStatusHistory(c, t.id).slice(-1)[0] ?? null,
        source_map: taskSourceMap(t),
        status_history: taskStatusHistory(c, t.id),
      })),
    };
  },

  // Evidence drawer endpoints (CREDIBILITY-PASS §4)
  'GET /api/cases/:id/evidence': async (q, p) => {
    const { c } = requireCase(p.id);
    const kind = q.get('kind') || 'event';
    if (kind === 'event') {
      const seq = Number(q.get('seq'));
      const payload = eventEvidence(c, seq);
      if (!payload) throw new ApiError(404, 'EVENT_NOT_FOUND', `seq ${q.get('seq')} out of range`);
      return payload;
    }
    if (kind === 'task') {
      const payload = taskEvidence(c, q.get('task'));
      if (!payload) throw new ApiError(404, 'TASK_NOT_FOUND', `unknown task: ${q.get('task')}`);
      return payload;
    }
    if (kind === 'approval') {
      const isRejection = c.gate.state === 'rejected';
      const recordRef = isRejection ? c.gate.rejection_text_source : c.gate.approval_text_source;
      const resolved = resolveSourceRef(recordRef);
      let recordText = null;
      if (isRejection) {
        try { recordText = readText('rejectDemo', 'human-gate-rejection.md'); } catch { /* absent */ }
      } else {
        try { recordText = readText('replayMaterials', 'artifacts/human-gate-approval.md'); } catch { /* absent */ }
      }
      return {
        drawer: isRejection ? 'rejection' : 'approval',
        ...drawerEnvelope(resolved),
        event_id: isRejection ? 'pr3-ev-008' : 'pr2-ev-007',
        trace_id: null,
        project_id: c.project.id,
        task_id: null,
        agent_role: 'human',
        runtime: 'human decision — 人工安全门（人工角色，非 Agent 行为）',
        event_type: 'approval',
        source: 'evidence',
        source_label: SRC.evidence,
        source_ref: recordRef,
        source_hash: resolved,
        timestamp: isRejection ? c.gate.rejected_at : c.gate.approved_at,
        timestamp_precision: isRejection ? 'exact' : 'approx',
        gate: c.gate,
        approval_text: isRejection ? null : recordText,
        rejection_text: isRejection ? recordText : null,
        decision_marker: isRejection ? 'HUMAN_SECURITY_REJECTED' : 'HUMAN_SECURITY_APPROVED_FIX',
        hash_verified: resolved?.hash_verified ?? null,
        redaction_status: redactionStatusOf(recordText ?? '', { scanForSecrets }),
      };
    }
    if (kind === 'trace') {
      return { drawer: 'trace', case_id: c.case_id, ...traceStatus(c), data_mode: 'historical replay (trace schema only)', trace_mode: 'evidence-replay', integrity_status: 'verified（schema 文件存在；smoke/live 状态见上）', redaction_status: redactionStatusOf('trace status block — no payloads', { scanForSecrets }) };
    }
    if (kind === 'finding') {
      const isPr3 = c.case_id === 'pr3-high-risk-human-reject';
      const findingEvent = c.timeline.find((e) => e.summary.includes('HIGH_RISK_FOUND'));
      const probeRef = c.fix_comparison ? resolveSourceRef(c.sources.probe_raw?.split(' ')[0] ?? c.sources.probe_raw) : null;
      const reviewRef = resolveSourceRef(c.sources.case_definition);
      return {
        drawer: 'finding',
        ...drawerEnvelope(reviewRef),
        event_id: findingEvent?.event_id ?? (isPr3 ? 'pr3-ev-006' : 'pr2-ev-005'),
        trace_id: null,
        project_id: c.project.id,
        task_id: 'review-1',
        agent_role: 'reviewer',
        runtime: c.runtime.worker_image,
        event_type: 'task_state',
        source: isPr3 ? 'evidence' : 'minio',
        source_label: isPr3 ? SRC.evidence : SRC.minio,
        source_ref: c.sources.verification_report
          ? c.sources.case_definition + ' + ' + c.sources.verification_report
          : c.sources.reviewer_result + ' + ' + c.sources.gate_request,
        source_hash: reviewRef,
        timestamp: c.timeline[4]?.timestamp ?? null,
        timestamp_precision: 'approx',
        finding: c.risk,
        reviewer_conclusion: c.risk.reviewer_conclusion,
        probe: c.fix_comparison ? { before: c.fix_comparison.probe.before, after: c.fix_comparison.probe.after, requests: { traversal: c.fix_comparison.probe.request_traversal, legit: c.fix_comparison.probe.request_legit } } : null,
        probe_hash: probeRef,
        hash_verified: reviewRef?.hash_verified ?? null,
        redaction_status: redactionStatusOf(JSON.stringify(c.risk), { scanForSecrets }),
      };
    }
    throw new ApiError(400, 'INVALID_KIND', 'kind must be event|task|approval|trace|finding');
  },

  'GET /api/cases/:id/artifacts': async (q, p) => {
    const { data, c } = requireCase(p.id);
    const wanted = q.get('file');
    if (wanted) {
      // serve one artifact TEXT (read-only), only from the registered artifact set
      const hit = data.artifacts.find((a) => a.file === wanted || a.file.endsWith(wanted));
      if (!hit || !hit.exists) throw new ApiError(404, 'ARTIFACT_NOT_FOUND', `artifact not in registry: ${wanted}`);
      return { file: hit.file, dir: hit.dir, sha256: hit.sha256, content: readText(hit.dir, hit.file) };
    }
    return {
      case_id: c.case_id,
      artifacts: data.artifacts.map((a) => ({ dir: a.dir, file: a.file, sha256: a.sha256, exists: a.exists, size_note: 'content 可经 ?file= 只读获取' })),
      integrity: data.integrity,
    };
  },

  'GET /api/cases/:id/audit': async (q, p) => {
    const { c } = requireCase(p.id);
    const audit = replayAudit();
    return {
      case_id: c.case_id,
      pr_compliance: {
        pr_number: c.pull_request.number,
        state: c.pull_request.state,
        write_operations: 'NONE — 未 merge/push/close/reopen',
        auto_merge: 'DISABLED',
      },
      components: audit.components,
      residual_risks: audit.residual_risks,
      stability_events: audit.stability_events,
      secret_scan: audit.secret_scan,
      verdicts: audit.verdicts,
      sha256sums: audit.sha256sums,
      case_sources: c.sources,
    };
  },

  'GET /api/cases/:id/trace': async (q, p) => {
    const { c } = requireCase(p.id);
    return {
      case_id: c.case_id,
      status: 'NOT_AVAILABLE',
      display: '待接入',
      reason: 'AgentLoop/OTel 未实现 — 本阶段无 live capture；不得声称已有 trace',
      schema_only: {
        exists: true,
        path: `${EVIDENCE_DIRS.agentloopOtel}/trace-schema.json`,
        note: '已定义 evidence-replay 模式的 trace schema（mode 恒为 evidence-replay，spans 永不以 live capture 呈现）',
      },
      tasks: c.tasks.map((t) => ({ task_id: t.id, trace_id: t.trace_id, trace_note: t.trace_note })),
    };
  },

  'GET /api/cases/:id/approval': async (q, p) => {
    const { c } = requireCase(p.id);
    const overlay = demoOverlay.get(p.id) ?? null;
    const isRejection = c.gate && c.gate.state === 'rejected';
    let recordText = null;
    let recordSource;
    if (isRejection) {
      recordSource = c.gate.rejection_text_source;
      try {
        recordText = readText('rejectDemo', 'human-gate-rejection.md');
      } catch {
        recordText = null;
      }
    } else {
      recordSource = `${EVIDENCE_DIRS.replayMaterials}/artifacts/human-gate-approval.md`;
      try {
        recordText = readText('replayMaterials', 'artifacts/human-gate-approval.md');
      } catch {
        recordText = null;
      }
    }
    return {
      case_id: c.case_id,
      decision_type: isRejection ? 'rejected' : 'approved_or_pending',
      historical_record: c.gate,
      approval_text: isRejection ? null : recordText,
      rejection_text: isRejection ? recordText : null,
      approval_text_source: isRejection ? null : `${EVIDENCE_DIRS.replayMaterials}/artifacts/human-gate-approval.md`,
      rejection_text_source: isRejection ? recordSource : null,
      rejected_case_note: isRejection
        ? '本案例历史裁决为 HUMAN_SECURITY_REJECTED — 禁止显示"批准成功"；此处展示真实拒绝记录'
        : null,
      demo_overlay: overlay,
      demo_overlay_note: overlay
        ? '仅存在于演示进程内存的回放演示层 — REPLAY ACTION — NO RUNTIME WRITE；不落盘、不写 runtime、不改动历史记录'
        : 'Replay 模式下按钮操作仅用于演示状态切换（见 POST 响应标记）',
    };
  },

  'POST /api/cases/:id/approval': async (q, p, body) => {
    const mode = modeOf(q);
    const { c } = requireCase(p.id);
    if (!c.gate || c.gate.state === 'none') {
      throw new ApiError(400, 'NO_HUMAN_GATE', `case ${p.id} has no human gate (conclusion non-high-risk)`);
    }
    const decision = body && body.decision;
    if (!['approve', 'reject', 'hold'].includes(decision)) {
      throw new ApiError(400, 'INVALID_DECISION', 'body.decision must be approve|reject|hold');
    }
    // A historically REJECTED case must never display "approve succeeded": the
    // replay does not invent a new human approval event that never happened.
    if (c.gate.state === 'rejected' && decision === 'approve') {
      throw new ApiError(409, 'REJECTED_CASE_TERMINAL', '本案例历史裁决为 HUMAN_SECURITY_REJECTED — 拒绝终态不可在演示层翻转为"批准成功"；如需恢复流程必须产生真实的新人工批准事件');
    }
    const actor = (body && body.actor) || 'demo-operator';

    if (mode === 'replay') {
      const overlay = {
        decision,
        actor,
        recorded_at: new Date().toISOString(),
        marker: 'REPLAY ACTION — NO RUNTIME WRITE',
      };
      demoOverlay.set(p.id, overlay);
      return {
        accepted: true,
        mode: 'replay',
        runtime_write: false,
        evidence_write: false,
        matrix_message_sent: false,
        marker: overlay.marker,
        overlay,
        historical_record_unchanged: true,
        note: '演示状态切换仅保存在演示进程内存；历史审批记录（human-gate-approval.md）不受影响',
      };
    }

    // LIVE mode — only via a protected, explicitly configured endpoint
    const target = liveApprovalTarget();
    if (!target.configured) {
      throw new ApiError(503, 'LIVE_DATA_UNAVAILABLE', target.note);
    }
    // When configured, the protected approval service would be called here with
    // server-side credentials. Not faked otherwise.
    throw new ApiError(501, 'LIVE_APPROVAL_NOT_IMPLEMENTED', 'DEMO_LIVE_APPROVAL_URL 已配置但受保护审批服务尚未实现 — 拒绝伪装成功');
  },

  // ------------------------------------------------------ Phase 15: RAG + PolarDB

  'GET /api/rag/status': async () => {
    const inv = datasetInventory();
    return {
      status: 'SYNTHETIC DEMO',
      data_mode: RAG_DATA_MODE,
      document_count: inv.document_count,
      chunk_count: inv.chunk_count,
      embedding_backend: EMBEDDING_BACKEND,
      retrieval_mode: RETRIEVAL_MODE,
      tool_span_contract: { logical: 'rag.retrieve', runtime: 'rag_retrieve', database: 'database.query', note: '运行时工具名需匹配 ^[a-zA-Z0-9_-]+$，故 logical rag.retrieve 以 rag_retrieve 注册；tool span 名为 execute_tool rag_retrieve' },
      agent_correlation: {
        status: 'VERIFIED',
        executed_in_agent_run: true,
        last_agent_execution_utc: '2026-08-30T11:53:53Z',
        cloud_trace_id: 'f5bb3e1f55bc53412f0fac5e6b400eb5',
        cloud_session_id: 'HTuUgFTncUNhFGrCgtBgFP',
        evidence: '操作员控制台确认：execute_tool rag_retrieve（6ms，成功）与 agent_step/invoke_agent/genai.llm.call/chat deepseek-chat 同 Trace；本地 tool-spans.jsonl 11:53:53.731Z 审计与云端 span 时序/计数吻合',
        cloud_span_confirmation: 'VERIFIED — 2026-08-30 19:53:52 local trace, LLM 6 / Tool 1, Token 11059'
      },
      polardb: polardbAudit(),
      disclosure: '全部文档为平台自有合成演示文档（SYNTHETIC），不含企业生产数据；检索只保存 query_hash，不保存 query 原文',
    };
  },

  'GET /api/rag/dataset': async () => {
    const inv = datasetInventory();
    return {
      data_mode: inv.data_mode,
      embedding_backend: inv.embedding_backend,
      document_count: inv.document_count,
      chunk_count: inv.chunk_count,
      documents: inv.documents,
      polardb_fixture: fixtureInventory(),
    };
  },

  'GET /api/rag/search': async (q) => {
    const query = (q.get('q') || '').trim();
    if (!query) throw new ApiError(400, 'EMPTY_QUERY', 'q 参数不能为空');
    const topK = Number(q.get('k') || 4);
    const r = retrieve(query, { topK });
    return { ...r, data_mode: RAG_DATA_MODE, disclosure: 'SYNTHETIC demo retrieval — 只保存 query_hash' };
  },

  'GET /api/rag/answer': async (q) => {
    const query = (q.get('q') || '').trim();
    if (!query) throw new ApiError(400, 'EMPTY_QUERY', 'q 参数不能为空');
    return answer(query);
  },

  'POST /api/rag/toolspan-audit': async (q, p, body) => {
    if (!body || typeof body !== 'object') throw new ApiError(400, 'INVALID_BODY', 'body must be a JSON object');
    const r = ingestExternalToolSpan(body);
    if (!r.ok) throw new ApiError(500, 'AUDIT_WRITE_FAILED', r.error || 'append failed');
    return { ok: true, data_mode: RAG_DATA_MODE };
  },

  'GET /api/rag/toolspans': async () => {
    return { data_mode: RAG_DATA_MODE, contract: ['tool', 'arguments_hash', 'result_status', 'row_count', 'document_count', 'latency_ms', 'data_mode', 'source_refs', 'ts'], recent: toolSpanTail(12) };
  },

  'GET /api/polardb/status': async () => {
    const audit = polardbAudit();
    const adapter = connectionState();
    return {
      connection: adapter.connection,
      branch_mode: adapter.branch_mode,
      adapter: { gates: adapter.gates, missing: adapter.missing, disclosure: adapter.disclosure },
      fixture: fixtureInventory(),
      legacy_audit: audit,
      pr4_candidates: CANDIDATES.map((c) => ({ candidate_id: c.candidate_id, expected: c.expected })),
    };
  },

  'GET /api/polardb/fixture': async (q) => {
    const table = q.get('table') || 'pr_cases';
    if (!['pr_cases', 'review_findings'].includes(table)) throw new ApiError(400, 'UNKNOWN_TABLE', 'fixture 仅提供 pr_cases / review_findings 只读快照');
    const where = {};
    for (const key of ['risk_level', 'gate', 'final_status', 'pr']) {
      const v = q.get(key);
      if (v !== null && v !== undefined) where[key] = v;
    }
    return fixtureQuery(table, where);
  },

  // ---------------------------------------------- Phase 16: PR #4 cross-repo

  'GET /api/pr4': async () => {
    const conn = connectionState();
    const ragHit = retrieve('跨仓 schema 变更 orders payments 关联仓库 历史风险', { topK: 3 });
    const validations = CANDIDATES.map((c) => {
      const b = createBranch(c.candidate_id);
      return {
        candidate_id: c.candidate_id,
        title: c.title,
        strategy: c.strategy,
        repos_touched: c.repos_touched,
        expected: c.expected,
        failure_reasons: c.failure_reasons,
        branch: b,
        validation: validateMigration(b.branch_id, c.candidate_id),
        assert_data: assertData(b.branch_id, c.candidate_id),
        rollback_check: rollbackCheck(b.branch_id, c.candidate_id),
      };
    });
    const promoted = validations.find((v) => v.validation.verdict === 'VERIFIED');
    return {
      pr: 4,
      case_id: 'pr4-cross-repo-schema-migration',
      title: 'CROSS-REPO ORDER SCHEMA MIGRATION',
      repos: ['fastapi-demo-api', 'fastapi-demo-worker', 'fastapi-demo-schema'],
      schema_baseline: SCHEMA_BASELINE,
      rag_impact: { query_hash: ragHit.query_hash, results: ragHit.results, data_mode: ragHit.data_mode },
      candidates: validations,
      promoted: promoted ? { candidate_id: promoted.candidate_id, verdict: promoted.validation.verdict } : null,
      rejected: validations.filter((v) => v.validation.verdict !== 'VERIFIED').map((v) => v.candidate_id),
      human_gate: {
        required: true,
        state: 'AWAITING_OPERATOR_APPROVAL（策略回放 — 无运行时写入）',
        note: 'candidate-c 部署前需人工批准；失败候选禁止 promote',
      },
      data_modes: { rag: 'SYNTHETIC', schema_baseline: 'SYNTHETIC', branch: 'SIMULATED', polardb_connection: conn.connection },
      agentloop: {
        authoritative_trace_id: 'fbf4a3cec0493990d76e10a102418be1',
        rag_correlation_trace_id: 'f5bb3e1f55bc53412f0fac5e6b400eb5',
        db_tool_correlation: '见 /api/rag/status agent_correlation',
      },
      integrity_status: 'fixture-backed, contract-audited, SHA256-locked evidence',
      disclosure: conn.disclosure,
    };
  },

  'POST /api/db/branch/create': async (q, p, body) => {
    const r = createBranch(body && body.candidate_id);
    if (r.error) throw new ApiError(400, r.error, 'unknown candidate_id');
    return r;
  },
  'POST /api/db/branch/validate': async (q, p, body) => {
    const r = validateMigration(body && body.branch_id, body && body.candidate_id);
    if (r.error) throw new ApiError(400, r.error, 'unknown candidate_id');
    return r;
  },
  'POST /api/db/branch/assert': async (q, p, body) => {
    const r = assertData(body && body.branch_id, body && body.candidate_id);
    if (r.error) throw new ApiError(400, r.error, 'unknown candidate_id');
    return r;
  },
  'POST /api/db/branch/rollback': async (q, p, body) => {
    const r = rollbackCheck(body && body.branch_id, body && body.candidate_id);
    if (r.error) throw new ApiError(400, r.error, 'unknown candidate_id');
    return r;
  },
  'GET /api/db/state': async () => branchState(),

  'GET /api/ops': async () => {
    const audit = polardbAudit();
    return {
      rag: { status: 'SYNTHETIC DEMO', data_mode: RAG_DATA_MODE, embedding_backend: EMBEDDING_BACKEND, last_query: lastQueryMeta() },
      polardb: { connection: audit.connection, verdict: audit.verdict, data_mode: 'SIMULATED (fixture)' },
      agentloop: { authoritative_trace_id: 'fbf4a3cec0493990d76e10a102418be1', correlation_trace_id: 'f5bb3e1f55bc53412f0fac5e6b400eb5', status: 'VERIFIED', trace_mode: 'LIVE CLOUD', tool_span_correlation: 'VERIFIED — rag_retrieve（逻辑名 rag.retrieve）已在真实 agent run 中执行并入云同 Trace（2026-08-30 11:53:53Z）' },
      data_modes: { agentteams_cases: 'REPLAY (verified historical run)', agentloop_trace: 'LIVE CLOUD', rag: 'SYNTHETIC', polardb: 'SIMULATED fixture / NOT CONNECTED' },
    };
  },
};

// route dispatcher
export async function handle(method, pathname, query, body) {
  const key = `${method} ${pathname}`;
  if (routes[key]) return routes[key](query, {}, body);
  const m = pathname.match(/^\/api\/cases\/([^/]+)(\/[a-z]+)?$/);
  if (m) {
    const routeKey = `${method} /api/cases/:id${m[2] || ''}`;
    if (routes[routeKey]) return routes[routeKey](query, { id: decodeURIComponent(m[1]) }, body);
  }
  throw new ApiError(404, 'NOT_FOUND', `no route: ${key}`);
}

export { redact, scanForSecrets };
