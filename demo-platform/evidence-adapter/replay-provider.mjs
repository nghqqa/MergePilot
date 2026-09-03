// evidence-adapter/replay-provider.mjs
// Replay provider — builds the two demo cases ENTIRELY from the locked evidence pack.
// Read-only. Every event carries source_ref provenance; provider startup validates
// that all referenced evidence files exist (replay integrity gate).
//
// Evidence anchors (all under D:\goai\p14-demo\evidence\):
//   replayMaterials PHASE14-WINDOWS-REPLAY-MATERIALS-20260829-195223  (primary replay pack)
//   fixAudit       PHASE14-WINDOWS-COPAW-HIGH-RISK-FIX-AUDIT-20260829-165813
//   fixVerify      PHASE14-WINDOWS-COPAW-HIGH-RISK-FIX-VERIFY-20260829-180548
//   finalLock      PHASE14-WINDOWS-AGENTTEAMS-COPAW-FINAL-20260829-130500

import { EVIDENCE_DIRS, integrityReport, artifactRegistry, loadProbeComparison, assertSourcesExist } from './evidence.mjs';

const D = EVIDENCE_DIRS;
const DATE = '2026-08-29';
const ts = (h, m = 0, s = 0) => `${DATE}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}Z`;

export const STATUS_LABELS = {
  pending: 'PENDING · 等待派发',
  running: 'RUNNING · 执行中',
  waiting_human: 'WAITING_HUMAN · 人工门停等',
  blocked: 'BLOCKED · 锁定',
  approved: 'APPROVED · 人工批准',
  rejected: 'REJECTED · 人工拒绝后锁定',
  locked: 'LOCKED · 永久锁定（不可派发）',
  completed: 'COMPLETED · 完成',
  failed: 'FAILED · 失败',
};

function snap(tasks, gate, phase, headline) {
  const out = {};
  for (const [id, t] of Object.entries(tasks)) {
    out[id] = { status: t.status, status_label: STATUS_LABELS[t.status] ?? t.status, locked_reason: t.locked_reason ?? null };
  }
  return { tasks: out, gate, phase, headline };
}

// Walk events, apply deltas, attach state_after snapshots.
function finalizeTimeline(events, initialTasks, initialGate, initialPhase) {
  const tasks = JSON.parse(JSON.stringify(initialTasks));
  let gate = initialGate;
  let phase = initialPhase;
  let headline = '初始状态 — 计划已建立，任务未派发';
  const initialState = snap(tasks, gate, phase, headline);
  events.forEach((ev, i) => {
    if (ev.delta) {
      if (ev.delta.tasks) {
        for (const [id, t] of Object.entries(ev.delta.tasks)) {
          if (t.status !== undefined) tasks[id].status = t.status;
          if (t.locked_reason !== undefined) tasks[id].locked_reason = t.locked_reason;
        }
      }
      if (ev.delta.gate !== undefined) gate = ev.delta.gate;
      if (ev.delta.phase !== undefined) phase = ev.delta.phase;
      if (ev.delta.headline) headline = ev.delta.headline;
      delete ev.delta;
    }
    ev.seq = i + 1;
    ev.state_after = snap(tasks, gate, phase, headline);
  });
  events.initial_state = initialState;
  return events;
}

function initialTask(status = 'pending', locked_reason = null) {
  return { status, locked_reason };
}

// ---------------------------------------------------------------------------
// PR #1 — normal autonomous collaboration (case A)
// ---------------------------------------------------------------------------
function buildPR1() {
  const initial = {
    'review-1': initialTask('pending', '等待 Leader 派发'),
    'fix-1': initialTask('pending', 'DAG 依赖：等待 review-1 完成'),
    'verify-1': initialTask('pending', 'DAG 依赖：等待 fix-1 完成'),
  };
  const events = [
    {
      event_id: 'pr1-ev-001', timestamp: ts(3, 42, 9), timestamp_precision: 'exact',
      agent_role: 'system', event_type: 'message', source: 'evidence',
      source_ref: `replayMaterials/05-timeline.md 阶段A`,
      summary: '复赛运行时启动：FileSync mirror_all 拉取 MinIO；Matrix re-login；controller 创建/纳管 4 个 copaw worker',
      delta: { phase: 'running', headline: '运行时就绪 — 4 个 copaw worker 被 controller 纳管' },
    },
    {
      event_id: 'pr1-ev-002', timestamp: ts(3, 59, 0), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `replayMaterials/01-case-pr1-normal.md DAG表`,
      matrix_event_id: '$nadwji6tQQkl4vSWDktWAFtWs_enjxAcg23927OKmrU',
      summary: 'Leader 委派 review-1 @reviewer（团队房间 m.mentions，真实 Matrix 事件）',
      detail: 'Leader 以 projectflow ready_nodes 生成可派发节点，taskflow delegate_task 经 Matrix m.mentions 委派；委派为真实消息投递（非内存调用）。',
      delta: { tasks: { 'review-1': { status: 'running', locked_reason: null } }, headline: 'review-1 已派发 → reviewer 执行中' },
    },
    {
      event_id: 'pr1-ev-003', timestamp: ts(4, 1, 24), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'message', source: 'evidence',
      source_ref: `replayMaterials/01-case-pr1-normal.md（reviewer 容器日志）`,
      summary: 'reviewer 消费消息（Created queue → agent 启动），开始独立审查',
    },
    {
      event_id: 'pr1-ev-004', timestamp: ts(4, 2, 17), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'task_state', source: 'evidence',
      source_ref: `finalLock/task-results.json + replayMaterials/05-timeline.md 阶段A`,
      summary: 'review-1 提交（TASK_COMPLETED @manager，STATUS: SUCCESS）',
      detail: '结论：bootstrap 范围审查通过，任务身份/指派/DAG 管道一致，无阻塞问题（非高危 → 不触发人工门）。',
      delta: { tasks: { 'review-1': { status: 'completed' }, 'fix-1': { locked_reason: '等待 Leader 派发（review-1 已完成）' } }, headline: 'review-1 完成（SUCCESS）— 结论非高危，自主流转' },
    },
    {
      event_id: 'pr1-ev-005', timestamp: ts(6, 17, 35), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `replayMaterials/01-case-pr1-normal.md DAG表`,
      summary: 'Leader 委派 fix-1 @fixer（真实 Matrix 事件）',
      delta: { tasks: { 'fix-1': { status: 'running', locked_reason: null } }, phase: 'fixing', headline: 'fix-1 已派发 → fixer 执行中' },
    },
    {
      event_id: 'pr1-ev-006', timestamp: ts(6, 19, 30), timestamp_precision: 'windowed',
      timestamp_note: '证据给出窗口：fix-1 委派 06:17:35 之后、verify-1 委派 06:20:42（标注"fix-1 完成后"）之前；结果事实来自 finalLock/task-results.json',
      agent_role: 'fixer', event_type: 'task_state', source: 'evidence',
      source_ref: `finalLock/task-results.json`,
      summary: 'fix-1 提交（STATUS: SUCCESS）',
      detail: '结论：完成 sandbox 修复步骤；本次无需整改（no remediation was required），就绪下游 verify-1。交付物 fix-findings.md。',
      delta: { tasks: { 'fix-1': { status: 'completed' }, 'verify-1': { locked_reason: '等待 Leader 派发（fix-1 已完成）' } }, headline: 'fix-1 完成（SUCCESS）' },
    },
    {
      event_id: 'pr1-ev-007', timestamp: ts(6, 20, 42), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `replayMaterials/01-case-pr1-normal.md DAG表`,
      summary: 'Leader 委派 verify-1 @verifier（fix-1 完成后，真实 Matrix 事件）',
      delta: { tasks: { 'verify-1': { status: 'running', locked_reason: null } }, phase: 'verifying', headline: 'verify-1 已派发 → verifier 执行中' },
    },
    {
      event_id: 'pr1-ev-008', timestamp: ts(6, 21, 17), timestamp_precision: 'exact',
      agent_role: 'verifier', event_type: 'task_state', source: 'matrix',
      source_ref: `replayMaterials/05-timeline.md 阶段A（TASK_COMPLETED: verify-1 @manager）`,
      summary: 'verify-1 完成：TASK_COMPLETED: verify-1 — 三棒全链闭环',
      detail: '结论：verify 验证通过（review-1 → fix-1 → verify-1 全 SUCCESS），交付物 verify-findings.md。',
      delta: { tasks: { 'verify-1': { status: 'completed' } }, headline: 'verify-1 完成 — 全链 SUCCESS' },
    },
    {
      event_id: 'pr1-ev-009', timestamp: ts(6, 21, 30), timestamp_precision: 'approx',
      agent_role: 'system', event_type: 'task_state', source: 'evidence',
      source_ref: `replayMaterials/01-case-pr1-normal.md 结果节 + finalLock/task-results.json`,
      summary: 'PROJECT COMPLETED — PR #1 普通协同案例全自主闭环（无人工介入）',
      detail: '三棒全部经真实 Matrix 消息触发；任务文件经 MinIO shared/ 双向同步；完整闭环重放证据见 e2eFull（20/20 SHA256SUMS）与 promotionFinal（17/17）。',
      delta: { phase: 'completed', headline: 'PR #1 项目完成 — 全自主 DAG 闭环（无人工门）' },
    },
  ];
  const timeline = finalizeTimeline(events, initial, 'none', 'running');

  return {
    case_id: 'pr1-normal-review',
    title: 'PR #1 — 普通代码审查（自主协同闭环）',
    positioning: '普通变更由 Agent 自主协作完成：Reviewer → Fixer → Verifier 全链无人工介入。',
    repository: 'nghqqa/fastapi-boilerplate-demo',
    pull_request: {
      number: 1,
      branch: 'wd1-pr1-bootstrap',
      state: 'open',
      write_actions: false,
      state_note: 'finalLock/pr-state-final.json：last_known_state=open；本 runtime 从未执行任何 PR 写操作',
    },
    project: { id: 'copaw-sandbox', status: 'completed' },
    team: { id: 'p14h2-wd (copaw workers)', size: 4 },
    runtime: { stack: 'AgentTeams agentteams-embedded:223ddc2', worker_image: 'copaw-worker:223ddc2-build1', mode: 'Docker Desktop, controller reconcile' },
    risk: { level: 'none', category: null, human_gate: 'none', note: 'review-1 结论非高危 → 不触发人工门（案例设计）' },
    agents: [
      { role: 'leader', agent_id: 'p14h2-copaw-worker-manager', matrix_id: '@p14h2-copaw-worker-manager:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:18682' },
      { role: 'reviewer', agent_id: 'p14h2-copaw-worker-reviewer', matrix_id: '@p14h2-copaw-worker-reviewer:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:14678' },
      { role: 'fixer', agent_id: 'p14h2-copaw-worker-fixer', matrix_id: '@p14h2-copaw-worker-fixer:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:18933' },
      { role: 'verifier', agent_id: 'p14h2-copaw-worker-verifier', matrix_id: '@p14h2-copaw-worker-verifier:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:11743' },
    ],
    dag: {
      nodes: [
        { id: 'review-1', label: 'review-1', sub: 'reviewer', kind: 'task' },
        { id: 'fix-1', label: 'fix-1', sub: 'fixer', kind: 'task' },
        { id: 'verify-1', label: 'verify-1', sub: 'verifier', kind: 'task' },
      ],
      edges: [{ from: 'review-1', to: 'fix-1' }, { from: 'fix-1', to: 'verify-1' }],
      human_gate_node: null,
    },
    tasks: [
      {
        id: 'review-1', project_id: 'copaw-sandbox', assignee: 'p14h2-copaw-worker-reviewer', runtime: 'copaw-worker:223ddc2-build1',
        started_at: ts(3, 59, 0), ended_at: ts(4, 2, 17),
        input_context: 'PR #1（wd1-pr1-bootstrap，bootstrap 引导 PR）范围审查：任务身份、指派与 DAG 管道一致性检查；预期非高危。',
        tool_calls: 'taskflow ack_task / submit_task；filesync（MinIO shared/）；GitHub 只读拉取',
        result_summary: 'STATUS: SUCCESS — Reviewed the copaw-sandbox project scope (bootstrap review-fix-verify DAG). Task identity, assignment, and DAG plumbing are consistent; no blocking issues found.',
        result_status: 'SUCCESS', effective: true,
        artifacts: [{ name: 'review-findings.md', source: 'finalLock/task-results.json (deliverables)' }],
        trace_id: null, trace_note: 'AgentLoop/OTel 未接入 — trace_id 不存在（如实显示"待接入"）',
        status_history_source: 'replayMaterials/01-case-pr1-normal.md + 05-timeline.md',
      },
      {
        id: 'fix-1', project_id: 'copaw-sandbox', assignee: 'p14h2-copaw-worker-fixer', runtime: 'copaw-worker:223ddc2-build1',
        started_at: ts(6, 17, 35), ended_at: ts(6, 19, 30), ended_at_precision: 'windowed',
        input_context: '对 review-1 结论执行常规修复步骤；本案例为 bootstrap 场景，无需整改。',
        tool_calls: 'taskflow ack/submit；filesync；本地工作区',
        result_summary: 'STATUS: SUCCESS — Completed the sandbox fix step... no remediation was required; ready for downstream verify-1.',
        result_status: 'SUCCESS', effective: true,
        artifacts: [{ name: 'fix-findings.md', source: 'finalLock/task-results.json (deliverables)' }],
        trace_id: null, trace_note: 'AgentLoop/OTel 未接入',
        status_history_source: 'finalLock/task-results.json',
      },
      {
        id: 'verify-1', project_id: 'copaw-sandbox', assignee: 'p14h2-copaw-worker-verifier', runtime: 'copaw-worker:223ddc2-build1',
        started_at: ts(6, 20, 42), ended_at: ts(6, 21, 17),
        input_context: '验证已完成链 review-1 → fix-1 → verify-1；常规回归。',
        tool_calls: 'taskflow ack/submit；filesync',
        result_summary: 'STATUS: SUCCESS — Verified the completed copaw-sandbox chain (review-1 -> fix-1 -> verify-1)... verification passes.',
        result_status: 'SUCCESS', effective: true,
        artifacts: [{ name: 'verify-findings.md', source: 'finalLock/task-results.json (deliverables)' }],
        trace_id: null, trace_note: 'AgentLoop/OTel 未接入',
        status_history_source: 'replayMaterials/05-timeline.md 阶段A',
      },
    ],
    timeline,
    gate: {
      gate_id: null, state: 'none',
      note: 'PR #1 结论非高危，不经过人工门 — 与 PR #2 对照展示"安全门是拓扑与策略的升级，而非另一套系统"。',
    },
    fix_comparison: null,
    context_events: [
      {
        timestamp: ts(3, 47, 25), timestamp_precision: 'exact',
        summary: 'copaw-sandbox kickoff 被 leader 消费；ready_nodes 因缺 plan.md 正确上报（未越权）',
        source_ref: 'finalLock/autonomous-timeline.json',
      },
      {
        timestamp: ts(4, 1, 4), timestamp_precision: 'exact',
        summary: '操作员经官方 projectflow 路径播种 plan.md；retry_attempt=2 发送后 leader 消费并自主完成全链',
        source_ref: 'finalLock/autonomous-timeline.json',
      },
    ],
    evidence_integrity: {
      sha256_dirs: ['e2eFull (20/20)', 'promotionFinal (17/17)', 'finalLock', 'replayMaterials'],
      secret_scan_clean: true,
      secret_scan_source: 'replayMaterials/SECRETS-SCAN.txt（模式 0 命中；真实凭据反向比对 0 命中）',
    },
    sources: {
      case_definition: `${D.replayMaterials}/01-case-pr1-normal.md`,
      timeline: `${D.replayMaterials}/05-timeline.md 阶段A`,
      task_results: `${D.finalLock}/task-results.json`,
      pr_state: `${D.finalLock}/pr-state-final.json`,
      e2e_replay: `${D.e2eFull}/SHA256SUMS (20/20)`,
      promotion: `${D.promotionFinal}/SHA256SUMS (17/17)`,
    },
  };
}

// ---------------------------------------------------------------------------
// PR #2 — CWE-22 high-risk human gate (case B)
// ---------------------------------------------------------------------------
function buildPR2(probe) {
  const initial = {
    'review-1': initialTask('pending', '等待 Leader 派发'),
    'fix-1': initialTask('pending', 'DAG 依赖：等待 review-1 结论'),
    'verify-1': initialTask('pending', 'DAG 依赖：等待 fix-1 完成'),
  };
  const events = [
    {
      event_id: 'pr2-ev-001', timestamp: ts(9, 49, 35), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `fixAudit/AUDIT.md 执行时间线`,
      matrix_event_id: '$IDofCrGE0os3RryOyFkF6R9qdX4cvU7bXL9nj1aiJac',
      summary: 'Leader 委派 review-1 @reviewer（build2 修复版 taskflow，m.mentions 命中，真实事件投递）',
      detail: '修复版 delegate_task 直接执行（无 kickoff、无手工消息）：project-scoped 新路径落位、m.mentions 命中 reviewer、commit 记录新 event_id。',
      delta: { tasks: { 'review-1': { status: 'running', locked_reason: null } }, phase: 'running', headline: 'review-1 已派发 → reviewer 独立安全审查中' },
    },
    {
      event_id: 'pr2-ev-002', timestamp: ts(9, 49, 35), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'message', source: 'evidence',
      source_ref: `fixAudit/AUDIT.md（reviewer docker logs）`,
      summary: 'reviewer 实时消费（Created queue → Consumer started → agent 启动）',
    },
    {
      event_id: 'pr2-ev-003', timestamp: ts(9, 49, 37), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'tool_call', source: 'minio',
      source_ref: `fixAudit/AUDIT.md（reviewer 侧 meta.json）`,
      summary: 'reviewer ack_task（acknowledged_at），meta → in_progress',
    },
    {
      event_id: 'pr2-ev-004', timestamp: ts(9, 55, 0), timestamp_precision: 'windowed',
      timestamp_note: '窗口 09:49–09:59（fixAudit/AUDIT.md：reviewer 抓取 diff、分析、成文）',
      agent_role: 'reviewer', event_type: 'message', source: 'evidence',
      source_ref: `fixAudit/AUDIT.md`,
      summary: 'reviewer 抓取 PR #2 diff、分析、成文；期间 manager 领导环自动催办一次（"assigned but not running"），reviewer 续跑',
    },
    {
      event_id: 'pr2-ev-005', timestamp: ts(10, 5, 0), timestamp_precision: 'approx',
      timestamp_note: '证据时间线出入的诚实标注：replayMaterials/05-timeline.md 记为 ~10:19，但执行报告 fixVerify/AUDIT.md 显示人工门记录写入与 fix-1 派发均为 10:06:24 且约束清单确认"批准后才派发 fix-1"，故 review-1 提交必在 10:06:24 之前；按执行报告窗口取 ~10:05。',
      agent_role: 'reviewer', event_type: 'task_state', source: 'matrix',
      source_ref: `replayMaterials/02-case-pr2-high-risk.md §1 + fixVerify/AUDIT.md`,
      matrix_event_id: '$0CmSNq-…（TASK_COMPLETED: review-1 @manager，证据中记录为前缀）',
      summary: 'HIGH_RISK_FOUND — review-1 提交：FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES（CWE-22 路径穿越任意文件读取）',
      detail: 'review-1/result.md 顶层协议标记：\nSTATUS: SUCCESS\nSUMMARY: Confirmed HIGH path traversal / arbitrary file read (CWE-22) ...\n         STATUS: FINDING_CONFIRMED, SEVERITY: HIGH, HUMAN_VERIFICATION_REQUIRED: YES\n受影响文件：backend/src/interfaces/api/v1/demo_high_risk.py 的 demo_download（用户可控 name 直接 os.path.join 到 DEMO_FILES_DIR，../ 可逃逸基目录）。',
      delta: {
        tasks: {
          'review-1': { status: 'completed' },
          'fix-1': { locked_reason: 'HIGH_RISK_FOUND 已确认 — 等待人工安全门判定' },
          'verify-1': { locked_reason: 'HIGH_RISK_FOUND 已确认 — 等待人工安全门判定' },
        },
        headline: 'HIGH_RISK_FOUND — CWE-22 确认，等待人工安全门',
      },
    },
    {
      event_id: 'pr2-ev-006', timestamp: ts(10, 5, 10), timestamp_precision: 'approx',
      timestamp_note: '紧随 review-1 高危结论（fixVerify/AUDIT.md 约束清单 #6：人工门批准后才派发 fix-1）',
      agent_role: 'system', event_type: 'task_state', source: 'evidence',
      source_ref: `replayMaterials/06-human-gate.md 设计原理`,
      summary: 'HUMAN_SECURITY_REVIEW_REQUIRED — 人工安全门触发，系统受控停等：Leader 不派发 fix-1/verify-1',
      detail: '高危漏洞的修复授权不由任何 Agent 自行决定：门未批期间 Leader 对 fix-1/verify-1 的 delegate_task 在策略上被禁止，系统停等并保留全部证据（fail-safe）。',
      delta: {
        gate: 'required',
        tasks: {
          'fix-1': { status: 'waiting_human', locked_reason: '人工安全门：HUMAN_SECURITY_REVIEW_REQUIRED — 未批准禁止派发' },
          'verify-1': { status: 'waiting_human', locked_reason: '人工安全门：HUMAN_SECURITY_REVIEW_REQUIRED — 未批准禁止派发' },
        },
        phase: 'waiting_human',
        headline: '⛔ 人工安全门 — 受控停等（Fixer/Verifier 锁定）',
      },
    },
    {
      event_id: 'pr2-ev-007', timestamp: ts(10, 6, 0), timestamp_precision: 'approx',
      timestamp_note: '批准记录与 fix-1 派发同时落盘于 10:06:24（fixVerify/AUDIT.md）；操作员批准动作时刻按执行报告窗口取 ~10:06',
      agent_role: 'human', event_type: 'approval', source: 'evidence',
      source_ref: `replayMaterials/artifacts/human-gate-approval.md + fixVerify/AUDIT.md`,
      summary: 'HUMAN_SECURITY_APPROVED_FIX — 操作员批准：确认 HIGH_RISK_FOUND 与 CWE-22 定性，授权派发 fix-1（最小修复+本地测试）与 verify-1（独立验证），禁止 merge/push/close/reopen，PR #2 保持 OPEN',
      detail: '人工门批准记录（human-gate-approval.md，12 项授权与禁令）：① HIGH_RISK_FOUND 结论确认；② CWE-22 定性确认；③ 授权派发 fix-1；④ 授权最小必要修复并运行测试；⑤ 授权 fix-1 完成后派发 verify-1；⑥ 授权独立验证与回归；⑦ 禁止自动 merge/push/close/reopen；⑧ PR #2 保持 OPEN；⑨ 不修改 PR #1/copaw-sandbox；⑩ 不触碰 OpenClaw 主线与 WSL；⑪ 不输出/落盘任何 password/token/API key/Cookie；⑫ 失败即停止并保留证据。',
      delta: {
        gate: 'approved',
        tasks: {
          'fix-1': { status: 'pending', locked_reason: null },
          'verify-1': { status: 'blocked', locked_reason: '依赖锁定：fix-1 未完成前 Verifier 禁止启动' },
        },
        phase: 'fixing',
        headline: '人工门已批准 → Fixer 解锁；Verifier 仍锁定（等 fix-1）',
      },
    },
    {
      event_id: 'pr2-ev-008', timestamp: ts(10, 6, 24), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `fixVerify/AUDIT.md 执行时间线`,
      matrix_event_id: '$JU09kIgwjvVJmEUSVSlNR8Va2-zhxsvtmFaZ3RIHuTM',
      summary: 'fix-1 委派 @fixer（人工门记录写入项目目录；计划修复 review-1 [~]→[x]、fix-1/verify-1 [~]→[ ]；m.mentions 命中 fixer）',
      delta: { tasks: { 'fix-1': { status: 'running', locked_reason: null } }, headline: 'fix-1 已派发 → fixer 最小修复中' },
    },
    {
      event_id: 'pr2-ev-009', timestamp: ts(10, 19, 0), timestamp_precision: 'approx',
      agent_role: 'fixer', event_type: 'tool_call', source: 'evidence',
      source_ref: `fixVerify/AUDIT.md + fixVerify/artifacts/fix-1.test-evidence.md`,
      summary: 'fixer 本地 clone PR #2 分支 demo/high-risk-human-gate（无远端凭证，仅本地作业）',
    },
    {
      event_id: 'pr2-ev-010', timestamp: ts(10, 21, 0), timestamp_precision: 'approx',
      agent_role: 'fixer', event_type: 'tool_call', source: 'evidence',
      source_ref: `fixVerify/artifacts/verify-1.before_probe_raw.txt + fix-1.test-evidence.md`,
      summary: `修复前探针：穿越请求 → HTTP ${probe.before.traversal_status}，泄露 ${probe.before.leaked_outside_secret ? 'TOP-SECRET-OUTSIDE-BASE（LEAKED_OUTSIDE_SECRET: True）' : '无'}；合法文件 → ${probe.before.legit_status}`,
      detail: `GET /demo/download?name=../outside/outside-secret.txt → ${probe.before.traversal_status}（漏洞确认）；GET /demo/download?name=welcome.txt → ${probe.before.legit_status}（合法基线）。`,
    },
    {
      event_id: 'pr2-ev-011', timestamp: ts(10, 22, 0), timestamp_precision: 'approx',
      agent_role: 'fixer', event_type: 'tool_call', source: 'evidence',
      source_ref: `replayMaterials/artifacts/fix.patch + fix-1.result.md`,
      summary: `最小修复应用（Path.resolve 归一化 + is_relative_to(DEMO_FILES_DIR) 包含性校验，单文件 12+/4−）+ 修复后探针：穿越 → HTTP ${probe.after.traversal_status} 无泄露；合法文件仍 ${probe.after.legit_status}`,
      delta: { headline: 'fixer 最小修复完成，探针转绿（404/404·200）— 待正式提交' },
    },
    {
      event_id: 'pr2-ev-012', timestamp: ts(10, 40, 8), timestamp_precision: 'exact',
      agent_role: 'fixer', event_type: 'message', source: 'controller',
      source_ref: `replayMaterials/07-disclosures.md 事件2 + fixVerify/AUDIT.md 三`,
      summary: '披露事件：tool_guard 审批超时→拒绝→清空 fixer 会话记忆（工作成果保留于 workspace，正式提交待 leader 催办）',
      detail: 'copaw 框架 tool_guard 交互审批与无人值守自治运行冲突；处置：依据人工门授权将 fixer/verifier 的 security.tool_guard.enabled=false 后重启（manager/reviewer 未改动）。已列入审计页残余风险。',
    },
    {
      event_id: 'pr2-ev-013', timestamp: ts(11, 5, 0), timestamp_precision: 'windowed',
      timestamp_note: '窗口 ~11:00–11:06（fixVerify/AUDIT.md：leader 催办后 ~11:0x 正式提交；verify-1 派发前）',
      agent_role: 'fixer', event_type: 'task_state', source: 'evidence',
      source_ref: `fixVerify/artifacts/fix-1.result.md`,
      summary: 'fix-1 正式提交：STATUS: SUCCESS / FIX_APPLIED / TESTS_PASSED；Leader 验收 effective=true，计划 fix-1→[x]',
      detail: '交付物：fix.patch、demo_high_risk.fixed.py、test-evidence.md（shared/projects/copaw-high-risk-human-gate/tasks/fix-1/workspace/）。',
      delta: {
        tasks: { 'fix-1': { status: 'completed' }, 'verify-1': { status: 'pending', locked_reason: null } },
        phase: 'verifying',
        headline: 'fix-1 完成并验收 — Verifier 解锁，可派发',
      },
    },
    {
      event_id: 'pr2-ev-014', timestamp: ts(11, 6, 0), timestamp_precision: 'approx',
      timestamp_note: '~11:0x（fixVerify/AUDIT.md）；verifier 消费时刻 11:06:25 为精确锚点',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `fixVerify/AUDIT.md 执行时间线`,
      matrix_event_id: '$t0dXkuWwmjdkA-Ao6H0Z3KhuYJLJ0nQPCq7w0Jr1aU8',
      summary: 'verify-1 委派 @verifier（fix-1 完成后；m.mentions 命中 verifier）',
      delta: { tasks: { 'verify-1': { status: 'running', locked_reason: null } }, headline: 'verify-1 已派发 → verifier 独立验证中' },
    },
    {
      event_id: 'pr2-ev-015', timestamp: ts(11, 6, 25), timestamp_precision: 'exact',
      agent_role: 'verifier', event_type: 'message', source: 'evidence',
      source_ref: `fixVerify/AUDIT.md（verifier docker logs）`,
      summary: 'verifier 实时消费（Created queue），开始独立验证（不信任 Fixer 结论）',
    },
    {
      event_id: 'pr2-ev-016', timestamp: ts(11, 15, 8), timestamp_precision: 'exact',
      agent_role: 'verifier', event_type: 'message', source: 'controller',
      source_ref: `replayMaterials/07-disclosures.md 事件2 + fixVerify/AUDIT.md 三`,
      summary: '披露事件：verifier 首轮同样被 tool_guard 清空；按人工门授权关闭其 guard 后，leader 催办 $uiQdNtIY…（11:23:30 消费）恢复闭环',
    },
    {
      event_id: 'pr2-ev-017', timestamp: ts(11, 24, 0), timestamp_precision: 'windowed',
      timestamp_note: '窗口 11:23:30（催办消费）– ~11:26（提交），按 verification-report.md 执行内容',
      agent_role: 'verifier', event_type: 'tool_call', source: 'evidence',
      source_ref: `fixVerify/artifacts/verify-1.verification-report.md`,
      summary: `独立验证执行：重 clone → git apply fix.patch（字节级一致）→ 自设计探针（修复前 ${probe.before.traversal_status} 泄露 / 修复后 ${probe.after.traversal_status} 拒绝；合法文件 ${probe.after.legit_status}）→ 回归检查`,
      detail: 'Verifier 不信任 Fixer 结论：补丁经 git apply --check 干净应用；重放产物与 fixer 交付 demo_high_risk.fixed.py 字节级一致；模块导入冒烟通过。',
    },
    {
      event_id: 'pr2-ev-018', timestamp: ts(11, 26, 0), timestamp_precision: 'approx',
      agent_role: 'verifier', event_type: 'task_state', source: 'evidence',
      source_ref: `fixVerify/artifacts/verify-1.result.md`,
      summary: 'verify-1 提交：STATUS: VERIFICATION_PASSED / SEVERITY: NONE / HUMAN_VERIFICATION_REQUIRED: NO / FIX_INDEPENDENTLY_VERIFIED REGRESSION_CHECK_PASSED',
      detail: '残余风险 NONE；修复经独立复现验证（非采信 Fixer 自述）。注：VERIFICATION_PASSED 字面值不在 store 白名单（状态枚举兼容问题，见审计页），验证内容本身完整可信。',
      delta: { tasks: { 'verify-1': { status: 'completed' } }, headline: 'verify-1 通过 — SEVERITY: NONE，独立验证完成' },
    },
    {
      event_id: 'pr2-ev-019', timestamp: ts(11, 26, 30), timestamp_precision: 'approx',
      agent_role: 'system', event_type: 'task_state', source: 'evidence',
      source_ref: `fixVerify/AUDIT.md 裁决 + replayMaterials/02-case-pr2-high-risk.md §5`,
      summary: 'PROJECT COMPLETED — 高危闭环完成；PR #2 保持 OPEN（未 merge/push/close/reopen）',
      detail: '裁决 COPAW_HIGH_RISK_FIXVERIFY_PASSED；修复以补丁交付物形态存在，是否进入远端分支属仓库维护者的人工决策，不在本系统授权范围。',
      delta: { phase: 'completed', headline: 'PR #2 高危闭环完成 — 人工门→修复→独立验证，PR 保持 OPEN' },
    },
  ];
  const timeline = finalizeTimeline(events, initial, 'none', 'running');

  return {
    case_id: 'pr2-high-risk-human-gate',
    title: 'PR #2 — CWE-22 路径穿越高危 · 人工安全门闭环',
    positioning: '发现高危风险时系统自动暂停并等待人工确认，批准后才继续修复和验证。',
    repository: 'nghqqa/fastapi-boilerplate-demo',
    pull_request: {
      number: 2,
      branch: 'demo/high-risk-human-gate',
      state: 'open',
      write_actions: false,
      state_note: '全程未 merge/push/close/reopen（fixVerify/AUDIT.md 约束清单 #7/8 与裁决）',
    },
    project: { id: 'copaw-high-risk-human-gate', status: 'completed' },
    team: { id: 'p14h2-wd (copaw workers, build2)', size: 4 },
    runtime: { stack: 'AgentTeams agentteams-embedded:223ddc2', worker_image: 'copaw-worker:223ddc2-build2', mode: 'Docker Desktop, controller reconcile' },
    risk: {
      level: 'high',
      category: 'CWE-22',
      human_gate: 'approved',
      affected_file: 'backend/src/interfaces/api/v1/demo_high_risk.py — demo_download',
      description: '用户可控 name 参数直接 os.path.join 到 DEMO_FILES_DIR，../ 可逃逸基目录，经 FileResponse 实现任意文件读取（路径穿越）。该缺陷为演示预置（PR #2 仓库内置 demo 占位数据）。',
      reviewer_conclusion: 'STATUS: SUCCESS / FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES（review-1 result.md 协议标记）',
      residual_after_fix: 'SEVERITY: NONE（verify-1 独立验证）',
    },
    agents: [
      { role: 'leader', agent_id: 'p14h2-copaw-worker-manager', matrix_id: '@p14h2-copaw-worker-manager:…:6167', runtime: 'copaw-worker:223ddc2-build2', console: '127.0.0.1:18682' },
      { role: 'reviewer', agent_id: 'p14h2-copaw-worker-reviewer', matrix_id: '@p14h2-copaw-worker-reviewer:…:6167', runtime: 'copaw-worker:223ddc2-build2', console: '127.0.0.1:14678' },
      { role: 'fixer', agent_id: 'p14h2-copaw-worker-fixer', matrix_id: '@p14h2-copaw-worker-fixer:…:6167', runtime: 'copaw-worker:223ddc2-build2', console: '127.0.0.1:18933' },
      { role: 'verifier', agent_id: 'p14h2-copaw-worker-verifier', matrix_id: '@p14h2-copaw-worker-verifier:…:6167', runtime: 'copaw-worker:223ddc2-build2', console: '127.0.0.1:11743' },
      { role: 'human', agent_id: 'operator (runtime owner)', matrix_id: '@admin（Element Web 127.0.0.1:18088 登录）', runtime: 'human console — 人工安全门审批', console: '127.0.0.1:18088' },
    ],
    dag: {
      nodes: [
        { id: 'review-1', label: 'review-1', sub: 'reviewer', kind: 'task' },
        { id: 'human-gate', label: 'HUMAN GATE', sub: '人工安全门', kind: 'gate' },
        { id: 'fix-1', label: 'fix-1', sub: 'fixer', kind: 'task' },
        { id: 'verify-1', label: 'verify-1', sub: 'verifier', kind: 'task' },
      ],
      edges: [{ from: 'review-1', to: 'human-gate' }, { from: 'human-gate', to: 'fix-1' }, { from: 'fix-1', to: 'verify-1' }],
      human_gate_node: 'human-gate',
    },
    tasks: [
      {
        id: 'review-1', project_id: 'copaw-high-risk-human-gate', assignee: 'p14h2-copaw-worker-reviewer', runtime: 'copaw-worker:223ddc2-build2',
        started_at: ts(9, 49, 35), ended_at: ts(10, 5, 0), ended_at_precision: 'approx',
        input_context: 'PR #2（demo/high-risk-human-gate）独立安全审查：聚焦 demo_download 的路径处理；输出协议化结论（FINDING_CONFIRMED / SEVERITY / HUMAN_VERIFICATION_REQUIRED）。',
        tool_calls: 'taskflow ack_task / submit_task；filesync；GitHub 只读拉取 PR diff',
        result_summary: 'STATUS: SUCCESS\nSUMMARY: Confirmed HIGH path traversal / arbitrary file read (CWE-22) ...\n         STATUS: FINDING_CONFIRMED, SEVERITY: HIGH, HUMAN_VERIFICATION_REQUIRED: YES',
        result_status: 'SUCCESS', effective: true,
        artifacts: [{ name: 'review-report.md', source: 'replayMaterials/02-case-pr2-high-risk.md §1（workspace/review-report.md 引用）' }],
        trace_id: null, trace_note: 'AgentLoop/OTel 未接入 — trace_id 不存在（如实显示"待接入"）',
        status_history_source: 'fixAudit/AUDIT.md + replayMaterials/02-case-pr2-high-risk.md',
      },
      {
        id: 'fix-1', project_id: 'copaw-high-risk-human-gate', assignee: 'p14h2-copaw-worker-fixer', runtime: 'copaw-worker:223ddc2-build2',
        started_at: ts(10, 6, 24), ended_at: ts(11, 5, 0), ended_at_precision: 'windowed',
        input_context: '人工门批准后执行最小必要修复：仅 demo_high_risk.py；本地测试；禁远端操作（无远端凭证）。',
        tool_calls: 'taskflow ack/submit；filesync；git clone（本地）；venv+pip；pytest；HTTP 探针；push_shared_path 交付物上传',
        result_summary: 'STATUS: SUCCESS / FIX_APPLIED / TESTS_PASSED — Path.resolve 归一化 + is_relative_to(DEMO_FILES_DIR) 包含性校验，越界返回 404；穿越请求 404 无泄露；合法文件仍 200。单文件最小变更（12+/4−）。',
        result_status: 'SUCCESS', effective: true,
        artifacts: [
          { name: 'fix.patch', source: 'replayMaterials/artifacts/fix.patch' },
          { name: 'demo_high_risk.fixed.py', source: 'fix-1.result.md DELIVERABLES' },
          { name: 'test-evidence.md', source: 'fixVerify/artifacts/fix-1.test-evidence.md' },
        ],
        trace_id: null, trace_note: 'AgentLoop/OTel 未接入',
        status_history_source: 'fixVerify/AUDIT.md 执行时间线',
      },
      {
        id: 'verify-1', project_id: 'copaw-high-risk-human-gate', assignee: 'p14h2-copaw-worker-verifier', runtime: 'copaw-worker:223ddc2-build2',
        started_at: ts(11, 6, 25), ended_at: ts(11, 26, 0), ended_at_precision: 'approx',
        input_context: '独立验证（不信任 Fixer 结论）：重 clone、重放补丁、自设计探针、回归检查。',
        tool_calls: 'taskflow ack/submit；filesync；git clone（本地）+ git apply --check；venv+pip+pytest；自设计 HTTP 探针；模块导入冒烟',
        result_summary: 'STATUS: VERIFICATION_PASSED / SEVERITY: NONE / HUMAN_VERIFICATION_REQUIRED: NO / FIX_INDEPENDENTLY_VERIFIED REGRESSION_CHECK_PASSED。',
        result_status: 'VERIFICATION_PASSED', effective: true, effective_note: 'store 白名单不含 VERIFICATION_PASSED 字面值（状态枚举兼容问题，如实披露）；结论以 result.md + verification-report.md 为准',
        artifacts: [
          { name: 'verification-report.md', source: 'fixVerify/artifacts/verify-1.verification-report.md' },
          { name: 'before_probe_raw.txt', source: 'fixVerify/artifacts/verify-1.before_probe_raw.txt' },
          { name: 'after_probe_raw.txt', source: 'fixVerify/artifacts/verify-1.after_probe_raw.txt' },
          { name: 'probe.py', source: 'verify-1.result.md DELIVERABLES' },
        ],
        trace_id: null, trace_note: 'AgentLoop/OTel 未接入',
        status_history_source: 'fixVerify/AUDIT.md 执行时间线',
      },
    ],
    timeline,
    gate: {
      gate_id: 'copaw-high-risk-human-gate:post-review',
      state: 'approved',
      trigger: {
        by: 'review-1',
        signals: ['FINDING_CONFIRMED', 'SEVERITY: HIGH', 'HUMAN_VERIFICATION_REQUIRED: YES'],
        triggered_at: `${DATE} ~10:05Z (approx)`,
      },
      approved_at: `${DATE} ~10:06Z (approx；批准记录与 fix-1 派发同时落盘 10:06:24Z，见 fixVerify/AUDIT.md)`,
      approver: 'operator (runtime owner) — 人工角色，经受保护控制台操作；非 Agent 行为',
      record_path: 'shared/projects/copaw-high-risk-human-gate/human-gate-approval.md（快照：replayMaterials/artifacts/human-gate-approval.md）',
      scope: [
        '确认 HIGH_RISK_FOUND 结论与 CWE-22 定性',
        '授权 Leader 派发 fix-1（最小修复 + 本地测试）',
        '授权 fix-1 完成后派发 verify-1（独立验证 + 回归）',
        '失败即停止并保留证据（fail-safe）',
      ],
      prohibitions: ['merge', 'push', 'close', 'reopen'],
      unblocks: ['fix-1', 'verify-1 (fix-1 完成后)'],
      approval_text_source: 'replayMaterials/artifacts/human-gate-approval.md',
    },
    fix_comparison: {
      headline: '安全修复前后对比 — 越界请求 200 → 404，合法文件 200 → 200',
      file: 'backend/src/interfaces/api/v1/demo_high_risk.py',
      patch_summary: 'Path.resolve() 归一化 + is_relative_to(DEMO_FILES_DIR) 包含性校验；越界/不存在返回 404；filename 使用 target.name 防头部注入。单文件 12 insertions / 4 deletions。',
      patch_source: 'replayMaterials/artifacts/fix.patch',
      tests: '仓库自带漏洞断言测试修复后转失败（预期结果：断言的是修复前 200/泄露行为）— test-evidence.md 有完整解释；模块导入冒烟通过',
      fixer_result: 'STATUS: SUCCESS / FIX_APPLIED / TESTS_PASSED',
      verifier_result: 'STATUS: VERIFICATION_PASSED / SEVERITY: NONE（独立重 clone + 字节级补丁核对 + 自设计探针）',
      pr_state: 'OPEN — 保持未合并（merge/push/close/reopen 被人工门明令禁止）',
      probe,
    },
    context_events: [
      {
        timestamp: ts(6, 25, 13), timestamp_precision: 'exact',
        summary: '首次高危委派静默丢失：平铺任务命名空间冲突使复用分支误判"已委派"，未发送任何 Matrix 通知，meta 记录陈旧 event_id',
        source_ref: 'replayMaterials/07-disclosures.md 事件4 + 05-timeline.md 阶段B',
      },
      {
        timestamp: ts(8, 9, 6), timestamp_precision: 'exact',
        summary: '操作员手工重投（@admin DM，无 m.mentions）同样不可达 — 触发只读审计',
        source_ref: 'replayMaterials/05-timeline.md 阶段B',
      },
      {
        timestamp: `${DATE} ~09:49Z`, timestamp_precision: 'approx',
        summary: '双根因修复（project-scoped 存储 + Matrix since-token 回放窗口/去重账本）+ 12 项单元测试（基线全失败→修复后全通过、零回归）→ build2 重放成功（即本案例主线）',
        source_ref: 'fixAudit/AUDIT.md + test_fix_audit.py（12 用例）',
      },
    ],
    evidence_integrity: {
      sha256_dirs: ['replayMaterials', 'fixAudit', 'fixVerify', 'finalLock'],
      secret_scan_clean: true,
      secret_scan_source: 'replayMaterials/SECRETS-SCAN.txt（模式 0 命中；真实凭据反向比对 0 命中；FS_ACCESS_KEY 命中为公开 worker 名标识，已澄清）',
    },
    sources: {
      case_definition: `${D.replayMaterials}/02-case-pr2-high-risk.md`,
      timeline: `${D.replayMaterials}/05-timeline.md 阶段C + ${D.fixVerify}/AUDIT.md 执行时间线`,
      gate_record: `${D.replayMaterials}/artifacts/human-gate-approval.md`,
      fix_result: `${D.fixVerify}/artifacts/fix-1.result.md`,
      verify_result: `${D.fixVerify}/artifacts/verify-1.result.md`,
      verification_report: `${D.fixVerify}/artifacts/verify-1.verification-report.md`,
      probe_raw: `${D.fixVerify}/artifacts/verify-1.before_probe_raw.txt + after_probe_raw.txt`,
      patch: `${D.replayMaterials}/artifacts/fix.patch`,
      audit_report: `${D.fixVerify}/AUDIT.md（裁决 COPAW_HIGH_RISK_FIXVERIFY_PASSED）`,
    },
  };
}

// ---------------------------------------------------------------------------
// PR #3 — CWE-78 critical command injection, human REJECT path (case C)
// Rejection is a HUMAN decision; after it the system must NOT continue:
// fix-1 stays rejected/never-dispatched, verify-1 stays locked, project blocked,
// PR #3 remains OPEN. Evidence: PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913.
// ---------------------------------------------------------------------------
function buildPR3() {
  const D3 = '2026-08-30';
  const ts3 = (h, m = 0, s = 0) => `${D3}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}Z`;
  const initial = {
    'review-1': initialTask('pending', '等待 Leader 派发'),
    'fix-1': initialTask('pending', 'DAG 依赖：等待 review-1 结论'),
    'verify-1': initialTask('pending', 'DAG 依赖：等待 fix-1 完成'),
  };
  const events = [
    {
      event_id: 'pr3-ev-001', timestamp: ts3(1, 20, 0), timestamp_precision: 'exact',
      agent_role: 'human', event_type: 'message', source: 'matrix',
      source_ref: 'rejectDemo/kickoff-send.txt',
      matrix_event_id: '$sdKbFeQ1HJG1xwHY-8egACN8UC0n6cFkTR3Fj5W7Ymo',
      summary: '操作员发送唯一 kickoff（项目 copaw-high-risk-human-reject，PR #3 安全审查；含 HARD RULE：高危即停等人工决定）',
      detail: 'kickoff 被 Leader 消费，但 Leader run 因运行时遗留 OTel 插桩缺陷（zz_agentloop_otel.py await 了 async generator）崩溃，未产生任何动作（见案例前置历史）；操作员随后做可逆修复并重发 continuation。',
    },
    {
      event_id: 'pr3-ev-002', timestamp: ts3(1, 25, 36), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'message', source: 'matrix',
      source_ref: 'rejectDemo/continuation-send.txt',
      matrix_event_id: '$tyjCz74ziFpChDk48tl0EVHZ1xJI-ZC92BernbLozIQ',
      summary: 'Leader 消费 continuation，开始自主执行（运行时已修复，恢复到既有 E2E 成功态）',
      delta: { phase: 'running', headline: 'Leader 开始自主执行 — 项目未创建' },
    },
    {
      event_id: 'pr3-ev-003', timestamp: ts3(1, 25, 53), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'minio',
      source_ref: 'rejectDemo/pre-rejection-project-meta.json',
      summary: 'Leader projectflow create_project copaw-high-risk-human-reject + 播种 plan.md（DAG review-1→fix-1→verify-1 就绪）',
      delta: { headline: '项目已创建 — DAG review-1 → fix-1 → verify-1 就绪' },
    },
    {
      event_id: 'pr3-ev-004', timestamp: ts3(1, 26, 5), timestamp_precision: 'approx',
      timestamp_note: 'reviewer 于 01:27:05 提交前开始执行；委派时刻按 Team Room TASK_COMPLETED 前窗口取 ~01:26',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: 'rejectDemo/agent-message-timeline.json',
      summary: 'Leader 自主 delegate review-1 @reviewer（Team Room m.mentions；非人工派发）',
      delta: { tasks: { 'review-1': { status: 'running', locked_reason: null } }, headline: 'review-1 已派发 → reviewer 独立安全审查中' },
    },
    {
      event_id: 'pr3-ev-005', timestamp: ts3(1, 26, 20), timestamp_precision: 'windowed',
      timestamp_note: '窗口 01:26–01:27（reviewer clone 分支 demo/high-risk-human-reject、审 diff 与分支文件）',
      agent_role: 'reviewer', event_type: 'tool_call', source: 'evidence',
      source_ref: 'rejectDemo/reviewer-findings.md',
      summary: 'reviewer 独立审查：clone PR #3 分支，审 demo_ping 端点 diff + 附带 exploit-repro 测试',
      detail: '受审文件：backend/src/interfaces/api/v1/demo_cmd_exec.py（用户可控 host 拼入 subprocess.run shell=True）；对照 tests/unit/test_demo_cmd_exec_injection.py 两个惰性复现测试。',
    },
    {
      event_id: 'pr3-ev-006', timestamp: ts3(1, 27, 5), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'task_state', source: 'matrix',
      source_ref: 'rejectDemo/reviewer-high-risk-result.md',
      summary: 'HIGH_RISK_FOUND — review-1 提交（TASK_COMPLETED @manager）：STATUS: HIGH_RISK_FOUND / SEVERITY: critical / HUMAN_VERIFICATION_REQUIRED: true（CWE-78 命令注入 → RCE）',
      detail: '结论（result.md 协议标记）：user-controlled host 被拼接进 ping 命令并以 shell 执行，;, &&, $(...) 可执行任意命令，命令与输出返回攻击者；交付物 findings.md。',
      delta: {
        tasks: {
          'review-1': { status: 'completed' },
          'fix-1': { locked_reason: 'HIGH_RISK_FOUND 已确认（critical）— 等待人工安全门判定' },
          'verify-1': { locked_reason: 'HIGH_RISK_FOUND 已确认（critical）— 等待人工安全门判定' },
        },
        headline: 'HIGH_RISK_FOUND — CWE-78 RCE 确认，等待人工安全门',
      },
    },
    {
      event_id: 'pr3-ev-007', timestamp: ts3(1, 27, 37), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'message', source: 'matrix',
      source_ref: 'rejectDemo/human-gate-request.json',
      matrix_event_id: '$UmoGLVKBbyFYgdw6o5IyVxc-Gs0qakVe2erHm6I7fRs',
      summary: 'HUMAN_SECURITY_REVIEW_REQUIRED — Leader 上报人工门并 STOP：不派发 fix-1/verify-1，PR #3 保持 OPEN',
      detail: 'Leader 同时已在 plan.md 预标记 fix-1/verify-1 为 [!]（gated on human decision）；kickoff 级 HARD RULE 生效：fix-1/verify-1 仅可在 @admin 显式决定（批准或拒绝）后行动。',
      delta: {
        gate: 'required',
        tasks: {
          'fix-1': { status: 'waiting_human', locked_reason: '人工安全门：HUMAN_SECURITY_REVIEW_REQUIRED — 未决定禁止派发' },
          'verify-1': { status: 'waiting_human', locked_reason: '人工安全门：HUMAN_SECURITY_REVIEW_REQUIRED — 未决定禁止派发' },
        },
        phase: 'waiting_human',
        headline: '⛔ 人工安全门 — 受控停等（Fixer/Verifier 锁定，等待批准或拒绝）',
      },
    },
    {
      event_id: 'pr3-ev-008', timestamp: ts3(1, 30, 42), timestamp_precision: 'exact',
      agent_role: 'human', event_type: 'approval', source: 'matrix',
      source_ref: 'rejectDemo/human-rejection-record.json',
      matrix_event_id: '$jEuOZNsiqMkzt27uFd0bNG8UD07SnFssLXOQtL05g9k',
      summary: 'HUMAN_SECURITY_REJECTED — 操作员正式拒绝修复（人工决定，非 Agent 行为）；绑定指令：fix-1 永不派发、verify-1 保持锁定、项目置 blocked、PR #3 保持 OPEN',
      detail: '拒绝记录写入 shared/projects/copaw-high-risk-human-reject/human-gate-rejection.md 并经 Matrix 投递给 Leader；含 6 条绑定效果（含禁止任何自动修复与 GitHub 写操作）。',
      delta: {
        gate: 'rejected',
        tasks: {
          'fix-1': { status: 'rejected', locked_reason: 'HUMAN_SECURITY_REJECTED — 人工拒绝修复，永不派发' },
          'verify-1': { status: 'locked', locked_reason: 'fix-1 已拒绝 — Verifier 永久锁定（无修复可验证）' },
        },
        headline: '🛑 人工拒绝 — 系统不绕过人工门：Fixer/Verifier 锁定，项目转 blocked',
      },
    },
    {
      event_id: 'pr3-ev-009', timestamp: ts3(1, 30, 55), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'minio',
      source_ref: 'rejectDemo/post-rejection-project-meta.json',
      summary: 'Leader projectflow pause_project — 项目状态 active → paused（blocked），NOT completed；DAG 不再推进',
      detail: 'Leader 同时在 plan.md 写入拒绝标注：fix-1 [!] REJECTED by human security review (HUMAN_SECURITY_REJECTED, 2026-08-30); never dispatch；verify-1 [!] locked, never dispatch (fix-1 rejected)。',
      delta: {
        phase: 'blocked',
        headline: '项目 BLOCKED — plan.md 拒绝标注落盘，DAG 停在 review-1 之后',
      },
    },
    {
      event_id: 'pr3-ev-010', timestamp: ts3(1, 31, 5), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'message', source: 'matrix',
      source_ref: 'rejectDemo/post-rejection-task-state.json',
      summary: '最终锁定状态回报：fix-1 REJECTED / verify-1 LOCKED / project paused / PR #3 OPEN 未修复 — 拒绝后无任何新派发',
      detail: 'Leader 回报确认：fixer/verifier 从未被调用；无 GitHub 写操作；拒绝记录已读回确认。系统按设计停在安全阻塞态，等待未来新的人工批准事件（本案例不添加）。',
      delta: { headline: 'HUMAN REJECT 闭环完成 — 系统安全停止，等待新的人工决策' },
    },
  ];
  const timeline = finalizeTimeline(events, initial, 'none', 'running');

  return {
    case_id: 'pr3-high-risk-human-reject',
    title: 'PR #3 — CWE-78 命令注入（critical）· 人工拒绝后安全阻塞',
    positioning: '严重风险被人工拒绝后，系统不绕过人工门继续修复：Fixer/Verifier 保持锁定，项目阻塞，PR 保持 OPEN，等待新的人工决策。',
    repository: 'nghqqa/fastapi-boilerplate-demo',
    pull_request: {
      number: 3,
      branch: 'demo/high-risk-human-reject',
      state: 'open',
      write_actions: false,
      state_note: '拒绝后 PR 保持 OPEN 且未修复（head ad267a6 全程不变、0 评论；pr-write-audit.json）',
    },
    project: { id: 'copaw-high-risk-human-reject', status: 'blocked' },
    team: { id: 'p14h2-copaw', size: 4 },
    runtime: { stack: 'AgentTeams agentteams-embedded:223ddc2', worker_image: 'copaw-worker:223ddc2-build1', mode: 'Docker Desktop, controller reconcile' },
    risk: {
      level: 'critical',
      category: 'CWE-78',
      human_gate: 'rejected',
      affected_file: 'backend/src/interfaces/api/v1/demo_cmd_exec.py — demo_ping (GET /demo/ping)',
      description: '用户可控 host 参数被 f-string 拼入 ping 命令并以 subprocess.run(shell=True) 执行；;, &&, $(...) 等 shell 元字符实现任意命令执行（RCE）。该缺陷为演示预置（故意漏洞文件 + 惰性 exploit-repro 测试，无真实凭据）。',
      reviewer_conclusion: 'STATUS: HIGH_RISK_FOUND / SEVERITY: critical / HUMAN_VERIFICATION_REQUIRED: true（review-1 result.md 协议标记，真实运行产出）',
      no_fix_note: '本案例无修复：人工拒绝后系统不执行任何自动修复，演示代码保持原样（不作为生产修复展示）。',
    },
    agents: [
      { role: 'leader', agent_id: 'p14h2-copaw-worker-manager', matrix_id: '@p14h2-copaw-worker-manager:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:18682' },
      { role: 'reviewer', agent_id: 'p14h2-copaw-worker-reviewer', matrix_id: '@p14h2-copaw-worker-reviewer:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:14678' },
      { role: 'fixer', agent_id: 'p14h2-copaw-worker-fixer', matrix_id: '@p14h2-copaw-worker-fixer:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:18933', lock: 'LOCKED — HUMAN_SECURITY_REJECTED 后永不派发' },
      { role: 'verifier', agent_id: 'p14h2-copaw-worker-verifier', matrix_id: '@p14h2-copaw-worker-verifier:…:6167', runtime: 'copaw-worker:223ddc2-build1', console: '127.0.0.1:11743', lock: 'LOCKED — 无修复可验证，永久锁定' },
      { role: 'human', agent_id: 'operator (runtime owner)', matrix_id: '@admin', runtime: 'human decision — 人工安全门拒绝', console: '127.0.0.1:18088' },
    ],
    dag: {
      nodes: [
        { id: 'review-1', label: 'review-1', sub: 'reviewer', kind: 'task' },
        { id: 'human-gate', label: 'HUMAN GATE', sub: '人工安全门（拒绝路径）', kind: 'gate' },
        { id: 'fix-1', label: 'fix-1', sub: 'fixer', kind: 'task' },
        { id: 'verify-1', label: 'verify-1', sub: 'verifier', kind: 'task' },
      ],
      edges: [{ from: 'review-1', to: 'human-gate' }, { from: 'human-gate', to: 'fix-1' }, { from: 'fix-1', to: 'verify-1' }],
      human_gate_node: 'human-gate',
    },
    tasks: [
      {
        id: 'review-1', project_id: 'copaw-high-risk-human-reject', assignee: 'p14h2-copaw-worker-reviewer', runtime: 'copaw-worker:223ddc2-build1',
        started_at: ts3(1, 26, 5), ended_at: ts3(1, 27, 5),
        input_context: 'PR #3（demo/high-risk-human-reject）独立安全审查：审 demo_ping 命令注入；输出协议化结论（HIGH_RISK_FOUND / SEVERITY / HUMAN_VERIFICATION_REQUIRED）。',
        tool_calls: 'taskflow ack_task / submit_task；git clone（本地）；filesync；GitHub 只读拉取 PR diff',
        result_summary: 'STATUS: SUCCESS\nSUMMARY: STATUS: HIGH_RISK_FOUND | SEVERITY: critical | HUMAN_VERIFICATION_REQUIRED: true | Confirmed critical OS command injection (CWE-78) / RCE in demo_ping ...',
        result_status: 'SUCCESS', effective: true,
        artifacts: [{ name: 'findings.md', source: 'rejectDemo/reviewer-findings.md' }],
        trace_id: null, trace_note: 'AgentLoop/OTel 未接入 — trace_id 不存在（如实显示"待接入"）',
        status_history_source: 'rejectDemo/reviewer-high-risk-result.md + agent-message-timeline-raw.json',
      },
      {
        id: 'fix-1', project_id: 'copaw-high-risk-human-reject', assignee: 'p14h2-copaw-worker-fixer', runtime: 'copaw-worker:223ddc2-build1',
        started_at: null, ended_at: null,
        input_context: null,
        tool_calls: '无 — 从未被派发、从未执行',
        result_summary: null,
        result_status: null, effective: false, effective_note: 'HUMAN_SECURITY_REJECTED — 人工拒绝修复，fix-1 永不派发（fixer-lock-audit.json：当日 0 次 consume）',
        artifacts: [{ name: 'post-rejection-plan.md（[!] REJECTED never dispatch 标注）', source: 'rejectDemo/post-rejection-plan.md' }],
        trace_id: null, trace_note: '未运行 — 无 trace',
        status_history_source: 'rejectDemo/fixer-lock-audit.json + post-rejection-task-state.json',
      },
      {
        id: 'verify-1', project_id: 'copaw-high-risk-human-reject', assignee: 'p14h2-copaw-worker-verifier', runtime: 'copaw-worker:223ddc2-build1',
        started_at: null, ended_at: null,
        input_context: null,
        tool_calls: '无 — 从未被派发、从未执行',
        result_summary: null,
        result_status: null, effective: false, effective_note: 'LOCKED — fix-1 已拒绝，无修复可验证；verify-1 永久锁定（verifier-lock-audit.json：当日 0 次 consume）',
        artifacts: [{ name: 'verifier-lock-audit.json', source: 'rejectDemo/verifier-lock-audit.json' }],
        trace_id: null, trace_note: '未运行 — 无 trace',
        status_history_source: 'rejectDemo/verifier-lock-audit.json + post-rejection-task-state.json',
      },
    ],
    timeline,
    gate: {
      gate_id: 'copaw-high-risk-human-reject:post-review',
      state: 'rejected',
      trigger: {
        by: 'review-1',
        signals: ['HIGH_RISK_FOUND', 'SEVERITY: critical', 'HUMAN_VERIFICATION_REQUIRED: true'],
        triggered_at: `${D3}T01:27:37Z (exact, Matrix event $UmoGLVKBbyFYgdw6o5IyVxc-Gs0qakVe2erHm6I7fRs)`,
      },
      rejected_at: `${D3}T01:30:42Z (exact, Matrix event $jEuOZNsiqMkzt27uFd0bNG8UD07SnFssLXOQtL05g9k)`,
      rejected_by: 'operator (runtime owner) — 人工决定，非 Agent 行为',
      decision: 'HUMAN_SECURITY_REJECTED',
      record_path: 'shared/projects/copaw-high-risk-human-reject/human-gate-rejection.md（快照：rejectDemo/human-gate-rejection.md）',
      scope: [
        '确认 HIGH_RISK_FOUND 结论与 CWE-78 定性',
        '拒绝修复：fix-1 永不派发',
        'verify-1 保持锁定（无修复可验证）',
        '项目置 blocked（projectflow pause_project），不完成',
        '失败即停止并保留证据（fail-safe）',
      ],
      prohibitions: ['fix-1 派发', 'verify-1 派发', '自动修复', 'merge', 'push', 'close', 'reopen'],
      unblocks: [],
      terminal_note: '终态：rejected → blocked。禁止从 rejected 自动转为 fixing/verifying/completed；仅新的显式人工批准事件可恢复（本案例不添加）。',
      rejection_text_source: 'rejectDemo/human-gate-rejection.md',
    },
    fix_comparison: null,
    context_events: [
      {
        timestamp: ts3(1, 18, 53), timestamp_precision: 'exact',
        summary: '运行时披露：容器重启后暴露昨日 AGENTLOOP-OTEL 实验（evidence-replay 阶段）遗留的 zz_agentloop_otel.py 插桩缺陷（await async generator），Leader 首次 run 崩溃；操作员以可逆方式停用插桩开关（/etc/agentloop-otel.json → .disabled，4 worker）并重启，恢复到既有 E2E 成功态；此后全部 agent 行为为真实运行',
        source_ref: 'rejectDemo/replay-material-addendum.md + README.md 披露节',
      },
      {
        timestamp: ts3(1, 27, 13), timestamp_precision: 'exact',
        summary: 'Leader 遭遇跨项目 task_id 同名（review-1 为多项目惯例命名），自主以 projectId 消歧解决；历史项目数据未触碰',
        source_ref: 'rejectDemo/agent-message-timeline-raw.json',
      },
      {
        timestamp: ts3(1, 27, 32), timestamp_precision: 'exact',
        summary: '旁支观察：reviewer 顺带完成昨日 OTel 实验遗留 obs-1（agentloop-obs-test），与本案 DAG 无关，不涉及 fixer/verifier',
        source_ref: 'rejectDemo/agent-message-timeline.json',
      },
    ],
    evidence_integrity: {
      sha256_dirs: ['rejectDemo (29/29)'],
      secret_scan_clean: true,
      secret_scan_source: 'rejectDemo/verdict.json（0 Secret 落盘）+ 平台启动时对 rejectDemo SHA256SUMS 实时重算',
    },
    sources: {
      case_definition: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/README.md',
      reviewer_result: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/reviewer-high-risk-result.md',
      gate_request: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/human-gate-request.json',
      rejection_record: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/human-gate-rejection.md',
      post_state: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/post-rejection-task-state.json',
      lock_audits: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/fixer-lock-audit.json + verifier-lock-audit.json',
      pr_audit: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/pr-write-audit.json',
      verdict: 'PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913/verdict.json (COPAW_HIGH_RISK_HUMAN_REJECTION_VERIFIED)',
    },
  };
}

// ---------------------------------------------------------------------------
// Provider assembly + integrity gate
// ---------------------------------------------------------------------------
let cache = null;

export function getReplayData({ refresh = false } = {}) {
  if (cache && !refresh) return cache;
  const probe = loadProbeComparison();
  const pr1 = buildPR1();
  const pr2 = buildPR2(probe);
  const pr3 = buildPR3();
  const integrity = integrityReport();
  const artifacts = artifactRegistry();

  // Replay integrity gate: every source_ref must point to an existing evidence file.
  const refs = [];
  for (const c of [pr1, pr2, pr3]) {
    refs.push(...c.timeline.map((e) => e.source_ref));
    refs.push(...Object.values(c.sources));
    if (c.gate && c.gate.record_path) refs.push(c.gate.approval_text_source ?? c.gate.rejection_text_source);
  }
  const missingRefs = assertSourcesExist(refs);

  cache = {
    provider: 'replay',
    banner: 'REPLAY — HISTORICAL VERIFIED RUN',
    generated_at: new Date().toISOString(),
    cases: { [pr1.case_id]: pr1, [pr2.case_id]: pr2, [pr3.case_id]: pr3 },
    integrity,
    artifacts,
    replay_integrity: {
      events_pr1: pr1.timeline.length,
      events_pr2: pr2.timeline.length,
      events_pr3: pr3.timeline.length,
      source_refs_checked: refs.length,
      missing_source_refs: missingRefs,
      ok: missingRefs.length === 0,
      note: '所有回放事件均锚定真实证据文件；时间精度以 exact/approx/windowed 如实标注',
    },
    redaction_note: 'PR #2 探针中的 TOP-SECRET-OUTSIDE-BASE / WELCOME-LEGIT-DEMO 为仓库内置演示占位串（非凭据），按规则显示',
  };
  return cache;
}

export function replayAudit() {
  const { integrity } = getReplayData();
  return {
    components: [
      { component: 'AgentTeams Controller', status: 'VERIFIED', source: 'agentteams-embedded:223ddc2（Docker reconcile 模式，worker 容器由 controller 创建/管理）', evidence: `${D.finalLock}/final-architecture.md` },
      { component: 'CoPaw Runtime', status: 'VERIFIED', source: 'copaw-worker:223ddc2-build2（含存储隔离与同步语义修复）', evidence: `${D.fixAudit}/Dockerfile.build2` },
      { component: 'Matrix (Tuwunel)', status: 'VERIFIED', source: 'p14h2-wd-ctrl:6167（全部 agent 通信 + 事件历史）', evidence: `${D.fixVerify}/ENTRYPOINTS.md` },
      { component: 'MinIO', status: 'VERIFIED', source: 'p14h2-wd-ctrl:9000（任务存储 shared/）', evidence: `${D.fixVerify}/ENTRYPOINTS.md` },
      { component: 'LLM Gateway (Higress)', status: 'VERIFIED', source: 'p14h2-wd-ctrl:8080（LLM 代理 + key-auth 多 consumer）', evidence: `${D.fixVerify}/ENTRYPOINTS.md` },
      { component: 'Docker Desktop', status: 'VERIFIED', source: 'Windows 宿主运行时（10/10 容器运行，零 auth 错误）', evidence: `${D.credRotation}/verdict.json` },
      { component: 'Credential Rotation', status: 'VERIFIED', source: 'CREDENTIAL_ROTATION_VERIFIED — 5/5 consumer 轮换、旧值吊销、deepseek 旧 key 已失效(401)', evidence: `${D.credRotation}/verdict.json` },
      { component: 'PR Auto Merge', status: 'DISABLED', source: '人工门禁令（merge/push/close/reopen 全禁）；本 runtime 无任何 PR 写操作', evidence: `${D.finalLock}/pr-state-final.json` },
      { component: 'Event Integrity', status: 'VERIFIED', source: `SHA256SUMS 重算 ${integrity.ok_files}/${integrity.total_files} 文件一致`, evidence: '启动时实时重算（见 /api/health）' },
      { component: 'PolarDB RAG', status: 'NOT_IMPLEMENTED', source: '未接入 — 无检索增强链路在运行', evidence: `${D.replayMaterials}/08-scope-boundary.md` },
      { component: 'Agentic Database Branch', status: 'NOT_IMPLEMENTED', source: '未实现；隔离为文件/容器层', evidence: `${D.replayMaterials}/08-scope-boundary.md` },
      { component: 'AgentLoop/OTel', status: 'PARTIAL_SCHEMA_ONLY', source: '未接入 live capture；仅定义 evidence-replay trace schema', evidence: `${D.agentloopOtel}/trace-schema.json` },
    ],
    residual_risks: [
      { item: 'MinIO shared tree 曾发生异常清空，已恢复，根因待查', detail: '2026-08-29 11:26–11:41 UTC shared/projects/.../tasks/ 整树变空；worker 本地副本从未丢失；以各 worker 提交原件回推恢复；疑似 controller 侧 fs-view reconcile 或 MinIO 生命周期策略，待查。', source: `${D.replayMaterials}/07-disclosures.md 事件1` },
      { item: 'tool_guard 曾导致会话超时，当前策略已调整', detail: '受保护工具调用需人工批准，无人值守下超时→拒绝→清空会话记忆（fixer 10:40:08 / verifier 11:15:08）；处置：仅对这两个 worker 关闭 guard 并重启（按人工门授权）；有人值守模式应重新开启。', source: `${D.replayMaterials}/07-disclosures.md 事件2` },
      { item: 'VERIFICATION_PASSED 状态枚举兼容问题', detail: 'store 白名单（RESULT_STATUSES）不含 VERIFICATION_PASSED，check_task 报 invalid result status；验证证据本身完整可信；待办：纳入白名单或统一映射。', source: `${D.replayMaterials}/07-disclosures.md 事件3` },
      { item: 'PolarDB RAG：未接入', detail: '未接通 PolarDB 作为向量/知识检索后端；Agent 知识来源仅为任务 spec、GitHub 公开内容与仓库文档。', source: `${D.replayMaterials}/08-scope-boundary.md` },
      { item: 'Agentic Database Branch：未实现', detail: '无数据库写时分支隔离；演示隔离为任务目录 + 独立 venv/clone 的文件/容器层。', source: `${D.replayMaterials}/08-scope-boundary.md` },
      { item: 'AgentLoop/OTel：未接入（部分：schema 已定义）', detail: '无 OpenTelemetry traces/metrics 导出；现有可观测为框架日志 + Matrix 事件历史；已定义 evidence-replay 模式 trace schema（PHASE14-WINDOWS-AGENTLOOP-OTEL-20260829-200307/trace-schema.json），spans 永不以 live capture 呈现。', source: `${D.agentloopOtel}/trace-schema.json` },
    ],
    stability_events: [
      { item: '首次高危委派静默丢失（已根因修复）', detail: '平铺命名空间冲突 + since-token 高水位/DM 误判双根因；只读审计定位 → 12 项单元测试（基线全失败→修复后全通过、零回归）→ build2 重放闭环。', source: `${D.replayMaterials}/07-disclosures.md 事件4 + ${D.fixAudit}/AUDIT.md` },
      { item: '凭据轮换', detail: '5/5 consumer 轮换验证、旧值吊销；曾暴露于转录的 deepseek key 已在平台侧死亡（401），新 key 端到端 200。', source: `${D.credRotation}/verdict.json` },
    ],
    sha256sums: integrity,
    secret_scan: {
      clean: true,
      source: `${D.replayMaterials}/SECRETS-SCAN.txt`,
      detail: '通用模式（password/secret/token/api_key/cookie 赋值、私钥、云厂商 token、JWT、长 hex）：0 命中；真实凭据值反向比对：0 命中（FS_ACCESS_KEY 命中 150 文件为公开 worker 名标识，已澄清）',
    },
    verdicts: [
      { phase: 'REPLAY-MATERIALS', verdict: 'REPLAY_MATERIALS_READY_FOR_DEMO_PLATFORM', source: `${D.replayMaterials}/VERDICT.txt` },
      { phase: 'HIGH-RISK-FIX-VERIFY', verdict: 'COPAW_HIGH_RISK_FIXVERIFY_PASSED', source: `${D.fixVerify}/VERDICT.txt` },
      { phase: 'HIGH-RISK-REJECT-DEMO', verdict: 'COPAW_HIGH_RISK_HUMAN_REJECTION_VERIFIED', source: `${D.rejectDemo}/verdict.json` },
      { phase: 'FINAL-LOCK', verdict: 'PHASE14_2H_WINDOWS_AGENTTEAMS_COPAW_SUBMISSION_READY', source: `${D.finalLock}/verdict.json` },
    ],
  };
}
