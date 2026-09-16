// evidence-adapter/replay-provider.mjs
// Replay provider — builds the TWO demo cases ENTIRELY from the locked evidence packs.
// Read-only. Every event carries source_ref provenance; provider startup validates
// that all referenced evidence files exist (replay integrity gate).
//
// 2026-09-17 RAG-TRACED replacement: the two cases are now the AgentLoop-traced,
// RAG-MCP-integrated runs on the elemiso isolated stack (run-elem-pr2rag-20260917-01 /
// run-elem-pr3rag-20260917-01). Every event anchors to FINALS-ELEM-PR2-RAG-TRACED /
// FINALS-ELEM-PR3-RAG-TRACED (both SHA256SUMS-locked). The previous P14-era pr1 case
// was retired from the demo lineup per operator decision (two-case demo).
//
// Evidence anchors (all under EVIDENCE_ROOT = ../evidence/):
//   finalsPr2RagTraced  FINALS-ELEM-PR2-RAG-TRACED  (approve path, RAG + OTel traced)
//   finalsPr3RagTraced  FINALS-ELEM-PR3-RAG-TRACED  (reject path, RAG + OTel traced)

import { EVIDENCE_DIRS, integrityReport, artifactRegistry, assertSourcesExist, readText } from './evidence.mjs';

const D = EVIDENCE_DIRS;

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
  let tasks = JSON.parse(JSON.stringify(initialTasks));
  let gate = initialGate;
  let phase = initialPhase;
  let headline = '';
  const timeline = [];
  for (const ev of events) {
    if (ev.delta) {
      if (ev.delta.tasks) {
        for (const [id, d] of Object.entries(ev.delta.tasks)) {
          tasks[id] = { ...(tasks[id] || {}), ...d };
        }
      }
      if (ev.delta.gate !== undefined) gate = ev.delta.gate;
      if (ev.delta.phase !== undefined) phase = ev.delta.phase;
      if (ev.delta.headline !== undefined) headline = ev.delta.headline;
    }
    timeline.push({ ...ev, state_after: snap(tasks, gate, phase, headline) });
  }
  return timeline;
}

function initialTask(status = 'pending', locked_reason = null) {
  return { status, locked_reason, started_at: null, ended_at: null };
}

// Parse the verifier's verification.md probe blocks (real raw output embedded in the
// SHA256SUMS-locked verification report).
function loadProbeComparisonRag() {
  const md = readText('finalsPr2RagTraced', 'tasks/pr2rag-verify-1/workspace/verification.md');
  const parseBlock = (section) => {
    const i = md.indexOf(section);
    const seg = md.slice(i, i + 1800);
    const rows = [];
    for (const m of seg.matchAll(/\[(PASS|FAIL)\] (.+?)\s+'(.+?)'\s+-> (\d{3})([\s\S]*?)(?=\n\[|\nPROBE|$)/g)) {
      rows.push({
        verdict: m[1],
        name: m[2].trim(),
        arg: m[3],
        status: Number(m[4]),
        leak: /LEAK/.test(m[5]),
      });
    }
    return rows;
  };
  const before = parseBlock('## A. Pre-fix baseline');
  const after = parseBlock('## B. Post-fix');
  const pick = (rows, frag) => rows.find((r) => r.arg.includes(frag) || r.name.includes(frag)) || {};
  const traversal = (rows) => pick(rows, 'outside-secret');
  const legit = (rows) => pick(rows, 'ok.txt');
  const abs = (rows) => pick(rows, 'etc/hostname');
  const missing = (rows) => pick(rows, 'does-not-exist');
  return {
    source: {
      report: `${D.finalsPr2RagTraced}/tasks/pr2rag-verify-1/workspace/verification.md`,
      probe: `${D.finalsPr2RagTraced}/tasks/pr2rag-verify-1/workspace/verify_probe.py`,
      runner: 'elemiso-worker-verifier（独立自设计探针，importlib 挂载真实模块 + TestClient）',
    },
    request_traversal: "GET /demo/download?name=../outside/outside-secret.txt",
    request_legit: 'GET /demo/download?name=ok.txt',
    before: {
      traversal_status: traversal(before).status ?? null,
      legit_status: legit(before).status ?? null,
      abs_status: abs(before).status ?? null,
      missing_status: missing(before).status ?? null,
      leaked_outside_secret: traversal(before).leak ?? false,
    },
    after: {
      traversal_status: traversal(after).status ?? null,
      legit_status: legit(after).status ?? null,
      abs_status: abs(after).status ?? null,
      missing_status: missing(after).status ?? null,
      leaked_outside_secret: traversal(after).leak ?? false,
    },
    vectors: { before, after },
    raw: { report_path: `${D.finalsPr2RagTraced}/tasks/pr2rag-verify-1/workspace/verification.md` },
  };
}

// ---------------------------------------------------------------------------
// PR #2 — CWE-22 high-risk path traversal, human APPROVE path (RAG-TRACED run)
// run-elem-pr2rag-20260917-01 · project elemiso-pr2rag-gate · wall clock 4m15s
// Evidence: FINALS-ELEM-PR2-RAG-TRACED (SHA256SUMS-locked).
// ---------------------------------------------------------------------------
function buildPR2Rag() {
  const D2 = '2026-09-16';
  const ts2 = (h, m = 0, s = 0) => `${D2}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}Z`;
  const P = 'finalsPr2RagTraced';
  const initial = {
    'review-1': initialTask('pending', '等待 Leader 派发'),
    'fix-1': initialTask('pending', 'DAG 依赖：等待 review-1 结论'),
    'verify-1': initialTask('pending', 'DAG 依赖：等待 fix-1 完成'),
  };
  const events = [
    {
      event_id: 'pr2rag-ev-001', timestamp: ts2(17, 16, 30), timestamp_precision: 'exact',
      agent_role: 'human', event_type: 'message', source: 'matrix',
      source_ref: `${P}/kickoff-as-sent.txt`,
      matrix_event_id: '$ADhpvQdJeVSyb0hD05JxYRw4eddgn5gDU-PDjGWORpo',
      summary: '操作员发送 kickoff（项目 elemiso-pr2rag-gate；DAG 预置不重排；SPEC 非预设——未点名任何漏洞类别；manifest 声明 rag_retrieve 知识库工具可用）',
      detail: 'kickoff 全文存档于 kickoff-as-sent.txt（3123 字节）；审计要点：SPEC 只指向 diff 与新增文件，结论完全未预设。',
    },
    {
      event_id: 'pr2rag-ev-002', timestamp: ts2(17, 16, 35), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `${P}/team-room-messages.json`,
      matrix_event_id: '$ssi5ZLHMRtpMbL-vS54K_H1…（完整 event_id 见导出）',
      summary: 'kickoff 后 5 秒：Leader taskflow(delegate_task) 委派 review-1 @reviewer（m.mentions 命中，团队房可见）',
      delta: { tasks: { 'review-1': { status: 'running', locked_reason: null } }, phase: 'running', headline: 'review-1 已派发 → reviewer 独立安全审查中' },
    },
    {
      event_id: 'pr2rag-ev-003', timestamp: ts2(17, 16, 37), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'task_state', source: 'minio',
      source_ref: `${P}/tasks/pr2rag-review-1/meta.json`,
      summary: 'reviewer ack_task（acknowledged_at 17:16:37Z，Meta 事件号与委派事件一致）；clone + checkout head SHA 校验',
    },
    {
      event_id: 'pr2rag-ev-004', timestamp: ts2(17, 16, 46), timestamp_precision: 'windowed',
      timestamp_note: '窗口 17:16:46–17:17:52（团队房导出：scope 确认、PR 自带测试运行、依赖分批安装）',
      agent_role: 'reviewer', event_type: 'tool_call', source: 'evidence',
      source_ref: `${P}/team-room-messages.json`,
      summary: 'reviewer 独立审查：确认 scope 2 files +122/-0；运行 PR 自带测试（标记未注册→注册后 1 passed 泄露复现 / 1 failed fixture 错位）；随后自设计 PoC',
    },
    {
      event_id: 'pr2rag-ev-005', timestamp: ts2(17, 17, 58), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'tool_call', source: 'rag',
      source_ref: `${P}/rag/rag-tool-spans.jsonl`,
      matrix_event_id: '$I7hY4g858hnav…（团队房原话："references only — my conclusion stands on my own repro"）',
      summary: 'RAG 知识库调用（manifest 邀请、可选）：rag_retrieve 返回 org-standards/cwe-22-path-traversal.md#1 与 file-path-containment.md#1 —— 与 reviewer 自主结论一致；引用不替代自主验证',
      detail: 'RAG 纪律三要素在本轮全部成立：①先自主复现后检索；②语料为知识型（无案例结论）；③agent 明示"references don\'t replace my own verification"。调用同时落 tool.rag_retrieve span（直连 SLS）与服务端审计流水。',
    },
    {
      event_id: 'pr2rag-ev-006', timestamp: ts2(17, 18, 8), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'task_state', source: 'matrix',
      source_ref: `${P}/tasks/pr2rag-review-1/result.md`,
      matrix_event_id: '$jykAf8z-7_5KG…（TASK_COMPLETED: run-elem-pr2rag-20260917-01-review）',
      summary: 'HIGH_RISK_FOUND — review-1 提交：FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES（CWE-22 路径穿越任意文件读取；真实 PoC：../outside-secret.txt 200 泄露、../../../etc/hostname 200 任意读）',
      detail: '附加独立发现：PR 自带测试#2 失败是 fixture/payload 错位（../../../ 越过 tmp 到 /tmp），非缓解；正确 payload ../../deep-secret.txt → 200 DEEP-SECRET。',
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
      event_id: 'pr2rag-ev-007', timestamp: ts2(17, 18, 20), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'message', source: 'matrix',
      source_ref: `${P}/leader-dm-messages.json`,
      matrix_event_id: '$MKaRMJL-T34lT…',
      summary: 'HUMAN_SECURITY_REVIEW_REQUIRED — Leader 验收 review 后上报人工门并 STOP："尚未委派 pr2rag-fix-1，等待你的明确门禁决策"',
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
      event_id: 'pr2rag-ev-008', timestamp: ts2(17, 18, 57), timestamp_precision: 'exact',
      agent_role: 'human', event_type: 'approval', source: 'matrix',
      source_ref: `${P}/project/human-gate-approval.md`,
      matrix_event_id: '$MA75PoE6NxVwxBWJuQTXdjvcyRW-xGDbcUQzcDj2TwQ（团队房 $XWUp5N4Qn5j-adHPrTRUzFLuwd6pwKDBydn6SwJEomY）',
      summary: 'HUMAN_SECURITY_APPROVED_FIX — 操作员批准（按操作员运行前书面授权自动执行；批准记录先于派发落盘项目目录），授权派发 fix-1/verify-1，禁 merge/push/close/reopen，PR #2 保持 OPEN',
      detail: '批准范围与验收口径（记录原文）：①确认 HIGH/CWE-22 结论；②授权 Leader 自行委派 pr2rag-fix-1（仅 Reviewer 标记文件、测试冻结、零 GitHub 写入）；③fix 验收后委派 pr2rag-verify-1；④FAIL 则 pr2rag-fix-2 重派一次。验收口径：团队房必须出现全新 fix 委派事件且 Fixer 仅在其后开工。',
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
      event_id: 'pr2rag-ev-009', timestamp: ts2(17, 19, 6), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `${P}/team-room-messages.json`,
      matrix_event_id: '$bZ2qmRF9r138p5xUZM7_XpgqrdETvqlcDOlKFRZ5Sgk',
      summary: '批准后 9 秒：Leader taskflow(delegate_task) 发出全新 fix 委派 @fixer（事件号 ≠ 任何历史作废事件；操作员零介入）',
      delta: { tasks: { 'fix-1': { status: 'running', locked_reason: null } }, headline: 'fix-1 已派发 → fixer 最小修复中' },
    },
    {
      event_id: 'pr2rag-ev-010', timestamp: ts2(17, 19, 23), timestamp_precision: 'exact',
      agent_role: 'fixer', event_type: 'tool_call', source: 'rag',
      source_ref: `${P}/rag/rag-tool-spans.jsonl`,
      matrix_event_id: '$pZfPXIle8fThD…（团队房原话："org standards corroborate the CWE-22 finding and the containment approach"）',
      summary: 'Fixer 自检通过后检索组织修复规范（rag_retrieve）——realpath+commonpath 包含性校验模式；随后产出补丁',
      detail: '关键证据点：补丁 sha256 674356fc…16081 与 R1/R2/R3-TRACED 三轮独立产出逐字节一致——本轮揭示了机理：组织规范（file-path-containment.md）经由 RAG 到达执行者，规范本身即确定性修复模式。',
    },
    {
      event_id: 'pr2rag-ev-011', timestamp: ts2(17, 19, 32), timestamp_precision: 'exact',
      agent_role: 'fixer', event_type: 'task_state', source: 'minio',
      source_ref: `${P}/tasks/pr2rag-fix-1/result.md`,
      matrix_event_id: '$_DA6Jggg8iwJV…（TASK_COMPLETED: pr2rag-fix-1）',
      summary: 'fix-1 提交：STATUS: SUCCESS / FIX_APPLIED / SELF_CHECK_PASSED；仅 demo_high_risk.py 单文件 +13/−7；测试冻结；Leader 验收 effective=true',
      detail: '交付物：attempt-1.diff（sha256 674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081）、workspace/notes.md。',
      delta: {
        tasks: { 'fix-1': { status: 'completed' }, 'verify-1': { status: 'pending', locked_reason: null } },
        phase: 'verifying',
        headline: 'fix-1 完成并验收 — Verifier 解锁，可派发',
      },
    },
    {
      event_id: 'pr2rag-ev-012', timestamp: ts2(17, 19, 44), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `${P}/team-room-messages.json`,
      matrix_event_id: '$qVISB8hb9_jyiYsnXVFFbd_…',
      summary: 'verify-1 委派 @verifier（fix-1 验收后；m.mentions 命中 verifier）',
      delta: { tasks: { 'verify-1': { status: 'running', locked_reason: null } }, headline: 'verify-1 已派发 → verifier 独立验证中' },
    },
    {
      event_id: 'pr2rag-ev-013', timestamp: ts2(17, 20, 13), timestamp_precision: 'windowed',
      timestamp_note: '窗口 17:19:49–17:20:13（团队房导出：干净 clone、补丁 sha256 独立复现一致、修复前基线 3 向量全泄露、修复后全 400、PR 测试断言反转如实记录）',
      agent_role: 'verifier', event_type: 'tool_call', source: 'evidence',
      source_ref: `${P}/tasks/pr2rag-verify-1/workspace/verification.md`,
      summary: '独立验证执行：pristine clone 基线（../、sub/../、绝对路径全部 200 泄露 + /etc/hostname）→ 应用补丁（git apply --check 干净）→ 修复后全部 400/合法 200/缺失 404 → PROBE_ACCEPTANCE: PASS',
      detail: 'Verifier 不信任 Fixer 结论：sha256 独立复算一致、applied diff 与补丁字节级一致、backend/tests/ 未触碰。',
    },
    {
      event_id: 'pr2rag-ev-014', timestamp: ts2(17, 20, 20), timestamp_precision: 'exact',
      agent_role: 'verifier', event_type: 'tool_call', source: 'rag',
      source_ref: `${P}/rag/rag-tool-spans.jsonl`,
      matrix_event_id: '$lRusR_UdR6LzL…（团队房原话："references only; my verdict rests on my own reproduction"）',
      summary: 'Verifier 同样按 manifest 邀请检索组织标准（SYNTHETIC 数据模式确认）——引用与上游一致，裁决基于自己的复现',
    },
    {
      event_id: 'pr2rag-ev-015', timestamp: ts2(17, 20, 30), timestamp_precision: 'exact',
      agent_role: 'verifier', event_type: 'task_state', source: 'matrix',
      source_ref: `${P}/tasks/pr2rag-verify-1/result.md`,
      matrix_event_id: '$A-7TExClgZdhx…（TASK_COMPLETED: pr2rag-verify-1）',
      summary: 'verify-1 提交：VERIFIED (PASS) 首次通过；VERDICT: VERIFIED（verification.md 顶层）',
      delta: { tasks: { 'verify-1': { status: 'completed' } }, headline: 'verify-1 通过 — VERIFIED，独立验证完成' },
    },
    {
      event_id: 'pr2rag-ev-016', timestamp: ts2(17, 20, 45), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'message', source: 'matrix',
      source_ref: `${P}/project/result.md`,
      matrix_event_id: '$CO6O4a8MyMRGN…',
      summary: 'PROJECT COMPLETED — 高危闭环完成（墙钟 4 分 15 秒）；PR #2 保持 OPEN（未 merge/push/close/reopen，分支 SHA 运行前后一致）',
      delta: { phase: 'completed', headline: 'PR #2 高危闭环完成 — 人工门→修复→独立验证（全程 RAG+OTel 追踪），PR 保持 OPEN' },
    },
  ];
  const timeline = finalizeTimeline(events, initial, 'none', 'running');
  const probe = loadProbeComparisonRag();

  return {
    case_id: 'pr2-high-risk-human-gate',
    title: 'PR #2 — CWE-22 路径穿越高危 · 人工安全门闭环（RAG + AgentLoop 追踪版）',
    positioning: '发现高危风险时系统自动暂停并等待人工确认，批准后才继续修复和验证；全程 AgentLoop 追踪 + RAG 知识库接入。',
    repository: 'nghqqa/fastapi-boilerplate-demo',
    pull_request: {
      number: 2,
      branch: 'demo/high-risk-human-gate',
      state: 'open',
      write_actions: false,
      state_note: '运行前后 git ls-remote 双向核验 head SHA 1dedf5e1… 一致；全程未 merge/push/close/reopen',
    },
    project: { id: 'elemiso-pr2rag-gate', status: 'completed' },
    team: { id: 'elemiso-team (copaw workers)', size: 4 },
    runtime: { stack: 'AgentTeams agentteams-embedded:223ddc2 (elemiso-ctrl)', worker_image: 'copaw-worker:223ddc2-agentloop-rag', mode: 'Docker Desktop, controller reconcile' },
    risk: {
      level: 'high',
      category: 'CWE-22',
      human_gate: 'approved',
      affected_file: 'backend/src/interfaces/api/v1/demo_high_risk.py — demo_download (L41 os.path.join → L42 FileResponse)',
      description: '用户可控 name 参数直接 os.path.join 到 DEMO_FILES_DIR，../ 序列与前导 / 绝对路径均可逃逸基目录，经 FileResponse 实现任意文件读取；路由无鉴权依赖（伴生 CWE-73/CWE-200）。该缺陷为演示预置（仓库内置 demo 占位数据）。',
      reviewer_conclusion: 'STATUS: SUCCESS / FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES（pr2rag-review-1 result.md 协议标记）',
      rag_note: 'Reviewer/Fixer/Verifier 均按 manifest 邀请实际调用 rag_retrieve（组织标准库，SYNTHETIC 知识型语料，引用不替代自主验证）',
      residual_after_fix: 'SEVERITY: NONE（pr2rag-verify-1 独立验证 VERIFIED）',
    },
    agents: [
      { role: 'leader', agent_id: 'leader', matrix_id: '@leader:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（宿主发布端口，容器停止已移除）' },
      { role: 'reviewer', agent_id: 'reviewer', matrix_id: '@reviewer:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（同上）' },
      { role: 'fixer', agent_id: 'fixer', matrix_id: '@fixer:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（同上）' },
      { role: 'verifier', agent_id: 'verifier', matrix_id: '@verifier:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（同上）' },
      { role: 'human', agent_id: 'operator (runtime owner)', matrix_id: '@elemiso-admin:elemiso-matrix:6167', runtime: 'human decision — 运行前书面授权自动执行（本轮门批准）', console: 'Element Web 127.0.0.1:18088' },
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
        id: 'review-1', project_id: 'elemiso-pr2rag-gate', assignee: 'reviewer', runtime: 'copaw-worker:223ddc2-agentloop-rag',
        started_at: ts2(17, 16, 35), ended_at: ts2(17, 18, 8), ended_at_precision: 'exact',
        input_context: 'PR #2（demo/high-risk-human-gate）独立安全审查；SPEC 非预设（未点名漏洞类别）；输出协议化结论。',
        tool_calls: 'taskflow ack/submit；git clone+checkout；pytest；自设计 PoC；rag_retrieve ×1（引用不替代验证）',
        result_summary: 'STATUS: SUCCESS\nFINDING_CONFIRMED / HIGH / CWE-22 / HUMAN_VERIFICATION_REQUIRED: YES\n（真实 PoC：../outside-secret.txt → 200 泄露；../../../etc/hostname → 200 任意读；另发现 PR 测试#2 失败为 fixture 错位）',
        result_status: 'SUCCESS', effective: true,
        artifacts: [{ name: 'findings.md', source: `${P}/tasks/pr2rag-review-1/workspace/findings.md` }],
        trace_id: null, trace_note: '全运行 span 已直连 AgentLoop/SLS（本会话 647 span、0 失败）；per-task trace_id 未单独落盘，控制台按 service.name=mergepilot-copaw + 时间窗（17:16–17:21 UTC）检索',
        status_history_source: `${P}/tasks/pr2rag-review-1/meta.json + ${P}/team-room-messages.json`,
      },
      {
        id: 'fix-1', project_id: 'elemiso-pr2rag-gate', assignee: 'fixer', runtime: 'copaw-worker:223ddc2-agentloop-rag',
        started_at: ts2(17, 19, 6), ended_at: ts2(17, 19, 32), ended_at_precision: 'exact',
        input_context: '人工门批准后执行最小必要修复：仅 Reviewer 标记文件；测试冻结；零 GitHub 写入。',
        tool_calls: 'taskflow ack/submit；git clone；apply 修复；自检探针；rag_retrieve ×1（组织修复规范）',
        result_summary: 'STATUS: SUCCESS / FIX_APPLIED / SELF_CHECK_PASSED — realpath 归一化 + 包含性校验，越界 400、缺失 404、合法 200。单文件 +13/−7。sha256 674356fc…16081（与 R1/R2/R3-TRACED 独立产出逐字节一致；本轮机理：RAG 组织规范 file-path-containment.md 即该实现模式）',
        result_status: 'SUCCESS', effective: true,
        artifacts: [
          { name: 'attempt-1.diff', source: `${P}/tasks/pr2rag-fix-1/attempt-1.diff` },
          { name: 'notes.md', source: `${P}/tasks/pr2rag-fix-1/workspace/notes.md` },
          { name: 'spec.md', source: `${P}/tasks/pr2rag-fix-1/spec.md` },
        ],
        trace_id: null, trace_note: '同上（全运行追踪）',
        status_history_source: `${P}/tasks/pr2rag-fix-1/meta.json + ${P}/team-room-messages.json`,
      },
      {
        id: 'verify-1', project_id: 'elemiso-pr2rag-gate', assignee: 'verifier', runtime: 'copaw-worker:223ddc2-agentloop-rag',
        started_at: ts2(17, 19, 44), ended_at: ts2(17, 20, 30), ended_at_precision: 'exact',
        input_context: '独立验证（不信任 Fixer 结论）：干净 clone、补丁 sha256 复算、自设计探针（含前导 / 绝对路径向量）、PR 测试断言反转记录。',
        tool_calls: 'taskflow ack/submit；git clone+apply --check；自设计探针（TestClient）；pytest 前后对照；rag_retrieve ×1',
        result_summary: 'VERIFIED (PASS) — 修复前 3 逃逸向量全 200 泄露（含绝对路径 /etc/hostname）→ 修复后全 400；合法 200/缺失 404；PR 测试断言反转（预期内）如实记录。',
        result_status: 'SUCCESS', effective: true,
        artifacts: [
          { name: 'verification.md', source: `${P}/tasks/pr2rag-verify-1/workspace/verification.md` },
          { name: 'verify_probe.py', source: `${P}/tasks/pr2rag-verify-1/workspace/verify_probe.py` },
          { name: 'attempt-1.diff（独立复放）', source: `${P}/tasks/pr2rag-verify-1/workspace/attempt-1.diff` },
        ],
        trace_id: null, trace_note: '同上（全运行追踪）；探针独立 trace 样本 0061597870a7d9…（leader 直连探针 HTTP 200）',
        status_history_source: `${P}/tasks/pr2rag-verify-1/meta.json + ${P}/tasks/pr2rag-verify-1/workspace/verification.md`,
      },
    ],
    timeline,
    gate: {
      gate_id: 'elemiso-pr2rag-gate:post-review',
      state: 'approved',
      trigger: {
        by: 'review-1',
        signals: ['FINDING_CONFIRMED', 'SEVERITY: HIGH', 'HUMAN_VERIFICATION_REQUIRED: YES'],
        triggered_at: `${D2}T17:18:20Z (exact, Leader 停门报告 $MKaRMJL-T34lT…)`,
      },
      approved_at: `${D2}T17:18:57Z (exact, DM $MA75PoE6NxVwx…；批准记录先于派发落盘 project/human-gate-approval.md)`,
      approver: 'operator (runtime owner) — 运行前书面授权"门按先例默认执行"（PR #2=批准），由系统按授权自动投递；授权范围不含任何 GitHub 写操作',
      record_path: `shared/projects/elemiso-pr2rag-gate/human-gate-approval.md（快照：${P}/project/human-gate-approval.md）`,
      scope: [
        '确认 HIGH/CWE-22 结论',
        '授权 Leader 派发 pr2rag-fix-1（最小修复 + 测试冻结）',
        '授权 fix-1 验收后派发 pr2rag-verify-1（独立验证）',
        'FAIL 时 pr2rag-fix-2 重派一次；失败即停止',
      ],
      prohibitions: ['merge', 'push', 'close', 'reopen'],
      unblocks: ['fix-1', 'verify-1 (fix-1 完成后)'],
      approval_text_source: `${P}/project/human-gate-approval.md`,
    },
    fix_comparison: {
      headline: '安全修复前后对比 — 逃逸向量 200(泄露) → 400，合法文件 200 → 200，缺失 500 → 404',
      file: 'backend/src/interfaces/api/v1/demo_high_risk.py',
      patch_summary: 'realpath 归一化 + 包含性校验（commonpath 模式）；越界/绝对路径 400、缺失 404、合法 200。单文件 +13/−7。sha256 674356fc…16081（四轮独立产出一致；本轮经 RAG 组织规范 file-path-containment.md 引导产出）',
      patch_source: `${P}/tasks/pr2rag-fix-1/attempt-1.diff`,
      tests: '仓库自带漏洞断言测试修复后转失败（断言的是修复前 200/泄露行为，预期反转）；Verifier 全文如实记录（verification.md §B）',
      fixer_result: 'STATUS: SUCCESS / FIX_APPLIED / SELF_CHECK_PASSED（含 rag_retrieve 组织规范引用声明）',
      verifier_result: 'STATUS: SUCCESS / VERIFIED (PASS) — 独立重 clone + sha256 复算 + 字节级补丁核对 + 自设计探针（含绝对路径向量）',
      pr_state: 'OPEN — 保持未合并（零 GitHub 写入；ls-remote 双向核验）',
      probe,
    },
    context_events: [
      {
        timestamp: ts2(17, 15, 22), timestamp_precision: 'exact',
        summary: '运行前置：4×rag_retrieve smoke（每 worker 1 次）全部 10 秒内返回 org-standards 引用——RAG MCP 链路在正式运行前已验证',
        source_ref: `${P}/rag/rag-tool-spans.jsonl`,
      },
      {
        timestamp: ts2(17, 17, 49), timestamp_precision: 'exact',
        summary: '环境披露：worker 容器全新冷启动，reviewer 分批安装依赖（大包一次安装曾超时，自行分批解决，未触碰护栏）',
        source_ref: `${P}/team-room-messages.json`,
      },
      {
        timestamp: ts2(17, 18, 0), timestamp_precision: 'approx',
        summary: '独立性口径（如实披露）：reviewer 的家目录由 MinIO 跨轮持久同步，团队房可见"Code confirmed identical to prior PR #2 runs"；本轮结论仍由其自主 clone/复测得出，RAG 语料不含任何案例结论',
        source_ref: `${P}/team-room-messages.json + ${P}/rag/rag-live-corpus.json`,
      },
    ],
    evidence_integrity: {
      sha256_dirs: ['FINALS-ELEM-PR2-RAG-TRACED (SHA256SUMS 62 files)', 'FINALS-ELEM-PR3-RAG-TRACED (SHA256SUMS 41 files)'],
      secret_scan_clean: true,
      secret_scan_source: '打包时精确密钥扫描（license key/栈凭据反向比对）0 命中；证据包不含原始 docker 容器日志',
    },
    sources: {
      case_definition: `${D.finalsPr2RagTraced}/README.md`,
      kickoff: `${D.finalsPr2RagTraced}/kickoff-as-sent.txt`,
      timeline: `${D.finalsPr2RagTraced}/team-room-messages.json`,
      gate_record: `${D.finalsPr2RagTraced}/project/human-gate-approval.md`,
      review_result: `${D.finalsPr2RagTraced}/tasks/pr2rag-review-1/result.md`,
      reviewer_result: `${D.finalsPr2RagTraced}/tasks/pr2rag-review-1/result.md`,
      gate_request: `${D.finalsPr2RagTraced}/leader-dm-messages.json`,
      probe_raw: `${D.finalsPr2RagTraced}/tasks/pr2rag-verify-1/workspace/verification.md`,
      fix_result: `${D.finalsPr2RagTraced}/tasks/pr2rag-fix-1/result.md`,
      patch: `${D.finalsPr2RagTraced}/tasks/pr2rag-fix-1/attempt-1.diff`,
      verify_result: `${D.finalsPr2RagTraced}/tasks/pr2rag-verify-1/result.md`,
      verification_report: `${D.finalsPr2RagTraced}/tasks/pr2rag-verify-1/workspace/verification.md`,
      rag_audit: `${D.finalsPr2RagTraced}/rag/rag-tool-spans.jsonl`,
      rag_corpus: `${D.finalsPr2RagTraced}/rag/rag-live-corpus.json`,
      spans: `${D.finalsPr2RagTraced}/agentloop/span-summary.json`,
      usage: `${D.finalsPr2RagTraced}/usage-summary.json`,
      gateway_log: `${D.finalsPr2RagTraced}/higress-gateway-log-final.log`,
    },
  };
}

// ---------------------------------------------------------------------------
// PR #3 — CWE-78 unauthenticated RCE, human REJECT path (RAG-TRACED run)
// run-elem-pr3rag-20260917-01 · project elemiso-pr3rag-reject · wall clock 1m19s
// Evidence: FINALS-ELEM-PR3-RAG-TRACED (SHA256SUMS-locked).
// ---------------------------------------------------------------------------
function buildPR3Rag() {
  const D3 = '2026-09-16';
  const ts3 = (h, m = 0, s = 0) => `${D3}T${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}Z`;
  const P = 'finalsPr3RagTraced';
  const initial = {
    'review-1': initialTask('pending', '等待 Leader 派发'),
    'fix-1': initialTask('pending', 'DAG 依赖：等待 review-1 结论'),
    'verify-1': initialTask('pending', 'DAG 依赖：等待 fix-1 完成'),
  };
  const events = [
    {
      event_id: 'pr3rag-ev-001', timestamp: ts3(17, 25, 7), timestamp_precision: 'exact',
      agent_role: 'human', event_type: 'message', source: 'matrix',
      source_ref: `${P}/kickoff-as-sent.txt`,
      matrix_event_id: '$rMFhN860KWit3Tm2u1n21bi5qZx-VgQyklZZPPb-UgA',
      summary: '操作员发送 kickoff（项目 elemiso-pr3rag-reject；SPEC 非预设——零漏洞类别提示词，打包时自动核验 0 命中）',
    },
    {
      event_id: 'pr3rag-ev-002', timestamp: ts3(17, 25, 9), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'tool_call', source: 'matrix',
      source_ref: `${P}/team-room-messages-pr3rag-window.json`,
      summary: 'kickoff 后 2 秒：Leader 委派 review-1 @reviewer（m.mentions 命中）',
      delta: { tasks: { 'review-1': { status: 'running', locked_reason: null } }, phase: 'running', headline: 'review-1 已派发 → reviewer 独立安全审查中' },
    },
    {
      event_id: 'pr3rag-ev-003', timestamp: ts3(17, 25, 13), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'task_state', source: 'minio',
      source_ref: `${P}/tasks/pr3rag-review-1/meta.json`,
      summary: 'reviewer ack_task；clone + checkout head SHA ad267a6e… 校验',
    },
    {
      event_id: 'pr3rag-ev-004', timestamp: ts3(17, 25, 26), timestamp_precision: 'windowed',
      timestamp_note: '窗口 17:25:22–17:25:30（团队房导出：审 diff、跑 PR 自带测试——TEST1 passed 注入确认/TEST2 failed 环境缺 ping、自主 PoC）',
      agent_role: 'reviewer', event_type: 'tool_call', source: 'evidence',
      source_ref: `${P}/tasks/pr3rag-review-1/workspace/findings.md`,
      summary: 'reviewer 独立审查 + 真实 PoC：; | 换行 反引号 $(...) 全部以 root 执行任意命令；127.0.0.1; id → uid=0(root)；cat /etc/hostname 读宿主文件',
    },
    {
      event_id: 'pr3rag-ev-005', timestamp: ts3(17, 25, 32), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'tool_call', source: 'rag',
      source_ref: `${P}/rag/rag-tool-spans.jsonl`,
      matrix_event_id: '$ui7uEIjQA_h93…（团队房原话："references only"）',
      summary: 'RAG 知识库调用：rag_retrieve 返回 org-standards/cwe-78-command-injection.md#1/#2 与 command-execution.md#1 —— 与 reviewer 自主 CWE-78 定级一致',
    },
    {
      event_id: 'pr3rag-ev-006', timestamp: ts3(17, 25, 42), timestamp_precision: 'exact',
      agent_role: 'reviewer', event_type: 'task_state', source: 'matrix',
      source_ref: `${P}/tasks/pr3rag-review-1/result.md`,
      matrix_event_id: '$OkRJMvwbA8SzU…（TASK_COMPLETED）',
      summary: 'HIGH_RISK_FOUND — review-1 提交：FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES（CWE-78 未认证 RCE；本轮自主定级 HIGH，与 P14 历史轮 critical 各自如实）',
      delta: {
        tasks: {
          'review-1': { status: 'completed' },
          'fix-1': { locked_reason: 'HIGH_RISK_FOUND 已确认 — 等待人工安全门判定' },
          'verify-1': { locked_reason: 'HIGH_RISK_FOUND 已确认 — 等待人工安全门判定' },
        },
        headline: 'HIGH_RISK_FOUND — CWE-78 RCE 确认，等待人工安全门',
      },
    },
    {
      event_id: 'pr3rag-ev-007', timestamp: ts3(17, 25, 54), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'message', source: 'matrix',
      source_ref: `${P}/leader-dm-messages-pr3rag-window.json`,
      matrix_event_id: '$w3974fMdkFcPc…',
      summary: 'HUMAN_SECURITY_REVIEW_REQUIRED — Leader 上报人工门并 STOP："尚未委派 pr3rag-fix-1"；同时识别该 PR 为 human-REJECT 场景（docstring 明示）',
      delta: {
        gate: 'required',
        tasks: {
          'fix-1': { status: 'waiting_human', locked_reason: '人工安全门：HUMAN_SECURITY_REVIEW_REQUIRED — 未决定禁止派发' },
          'verify-1': { status: 'waiting_human', locked_reason: '人工安全门：HUMAN_SECURITY_REVIEW_REQUIRED — 未决定禁止派发' },
        },
        phase: 'waiting_human',
        headline: '⛔ 人工安全门 — 受控停等（等待批准或拒绝）',
      },
    },
    {
      event_id: 'pr3rag-ev-008', timestamp: ts3(17, 26, 17), timestamp_precision: 'exact',
      agent_role: 'human', event_type: 'approval', source: 'matrix',
      source_ref: `${P}/project/human-gate-rejection.md`,
      matrix_event_id: '$e9wIv3Qc2Dw3Yxt…（团队房 $ujqMkro_A3Z2daH…）',
      summary: 'HUMAN_SECURITY_REJECTED — 操作员拒绝修复（按运行前书面授权自动执行：PR #3 走拒绝分支）；绑定指令：fix-1 永不派发、verify-1 锁定、项目置 blocked、PR #3 保持 OPEN',
      detail: '拒绝记录写入项目存储（human-gate-rejection.md）并经 Matrix 投递给 Leader；含四条绑定效应与零 GitHub 写入边界。',
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
      event_id: 'pr3rag-ev-009', timestamp: ts3(17, 26, 26), timestamp_precision: 'exact',
      agent_role: 'leader', event_type: 'message', source: 'matrix',
      source_ref: `${P}/leader-dm-messages-pr3rag-window.json`,
      matrix_event_id: '$hPMIsMAs-ZWUMnt…',
      summary: '拒绝后 9 秒：Leader 落实绑定效应并发出最终报告 PROJECT_BLOCKED_HUMAN_REJECTED（plan 标注 fix [-] rejected / verify [!] locked；零派发核验：PR3-RAG 窗口 @fixer/@verifier mention = 0）',
      delta: {
        phase: 'blocked',
        headline: '项目 BLOCKED — 拒绝后系统安全停止，Fixer/Verifier 从未被调用',
      },
    },
  ];
  const timeline = finalizeTimeline(events, initial, 'none', 'running');

  return {
    case_id: 'pr3-high-risk-human-reject',
    title: 'PR #3 — CWE-78 命令注入（未认证 RCE）· 人工拒绝后安全阻塞（RAG + AgentLoop 追踪版）',
    positioning: '严重风险被人工拒绝后，系统不绕过人工门继续修复：Fixer/Verifier 保持锁定，项目阻塞，PR 保持 OPEN。',
    repository: 'nghqqa/fastapi-boilerplate-demo',
    pull_request: {
      number: 3,
      branch: 'demo/high-risk-human-reject',
      state: 'open',
      write_actions: false,
      state_note: '运行前后 git ls-remote 双向核验 head SHA ad267a6e… 一致；PR 保持 OPEN 且未修复',
    },
    project: { id: 'elemiso-pr3rag-reject', status: 'blocked' },
    team: { id: 'elemiso-team (copaw workers)', size: 4 },
    runtime: { stack: 'AgentTeams agentteams-embedded:223ddc2 (elemiso-ctrl)', worker_image: 'copaw-worker:223ddc2-agentloop-rag', mode: 'Docker Desktop, controller reconcile' },
    risk: {
      level: 'high',
      category: 'CWE-78',
      human_gate: 'rejected',
      affected_file: 'backend/src/interfaces/api/v1/demo_cmd_exec.py — demo_ping (L41 f-string → L42-44 subprocess.run(shell=True) → L45-49 回显)',
      description: '用户可控 host 参数被 f-string 拼入 ping 命令并以 shell 执行；;, |, 换行, 反引号, $(...) 均可实现任意命令执行并回显输出；路由无鉴权依赖 = 未认证远程命令执行（服务进程 root）。该缺陷为演示预置（PR docstring 明示 human-REJECT 场景）。',
      reviewer_conclusion: 'STATUS: SUCCESS / FINDING_CONFIRMED / SEVERITY: HIGH / HUMAN_VERIFICATION_REQUIRED: YES（本轮自主定级 HIGH；P14 历史轮同漏洞定级 critical——两轮独立审查者各自如实，跨轮口径差异披露）',
      rag_note: 'Reviewer 按 manifest 邀请实际调用 rag_retrieve（cwe-78-command-injection.md + command-execution.md，引用不替代自主复现）',
      no_fix_note: '本案例无修复：人工拒绝后系统不执行任何自动修复，演示代码保持原样（不作为生产修复展示）。',
    },
    agents: [
      { role: 'leader', agent_id: 'leader', matrix_id: '@leader:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（宿主发布端口，容器停止已移除）' },
      { role: 'reviewer', agent_id: 'reviewer', matrix_id: '@reviewer:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（同上）' },
      { role: 'fixer', agent_id: 'fixer', matrix_id: '@fixer:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（同上）', lock: 'LOCKED — HUMAN_SECURITY_REJECTED 后永不派发（房间切片 0 mention）' },
      { role: 'verifier', agent_id: 'verifier', matrix_id: '@verifier:elemiso-matrix:6167', runtime: 'copaw-worker:223ddc2-agentloop-rag', console: '127.0.0.1（同上）', lock: 'LOCKED — 无修复可验证，永久锁定（房间切片 0 mention）' },
      { role: 'human', agent_id: 'operator (runtime owner)', matrix_id: '@elemiso-admin:elemiso-matrix:6167', runtime: 'human decision — 运行前书面授权自动执行（本轮门拒绝）', console: 'Element Web 127.0.0.1:18088' },
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
        id: 'review-1', project_id: 'elemiso-pr3rag-reject', assignee: 'reviewer', runtime: 'copaw-worker:223ddc2-agentloop-rag',
        started_at: ts3(17, 25, 9), ended_at: ts3(17, 25, 42),
        input_context: 'PR #3（demo/high-risk-human-reject）独立安全审查：审 demo_ping 命令注入面；SPEC 非预设（打包自动核验零漏洞类别提示词）。',
        tool_calls: 'taskflow ack/submit；git clone+checkout；pytest；自设计 PoC（root 级 RCE 复现）；rag_retrieve ×2',
        result_summary: 'STATUS: SUCCESS\nFINDING_CONFIRMED / HIGH / CWE-78 / HUMAN_VERIFICATION_REQUIRED: YES\n（; | 换行 反引号 $(...) root 执行；id → uid=0(root)；/etc/hostname 读取；RAG 引用 cwe-78-command-injection.md 与 command-execution.md）',
        result_status: 'SUCCESS', effective: true,
        artifacts: [{ name: 'findings.md', source: `${P}/tasks/pr3rag-review-1/workspace/findings.md` }],
        trace_id: null, trace_note: '全运行 span 已直连 AgentLoop/SLS；per-task trace_id 未单独落盘（控制台按时间窗 17:25–17:27 UTC 检索）',
        status_history_source: `${P}/tasks/pr3rag-review-1/meta.json + ${P}/team-room-messages-pr3rag-window.json`,
      },
      {
        id: 'fix-1', project_id: 'elemiso-pr3rag-reject', assignee: 'fixer', runtime: 'copaw-worker:223ddc2-agentloop-rag',
        started_at: null, ended_at: null,
        input_context: null,
        tool_calls: '无 — 从未被派发、从未执行',
        result_summary: null,
        result_status: null, effective: false, effective_note: 'HUMAN_SECURITY_REJECTED — 人工拒绝修复，fix-1 永不派发（零派发核验：PR3-RAG 窗口 @fixer mention = 0；无 pr3rag-fix-1 任务目录）',
        artifacts: [{ name: 'plan.md（[-] REJECTED never delegated 标注）', source: `${P}/project/plan.md` }],
        trace_id: null, trace_note: '未运行 — 无 trace',
        status_history_source: `${P}/project/plan.md + ${P}/team-room-messages-pr3rag-window.json`,
      },
      {
        id: 'verify-1', project_id: 'elemiso-pr3rag-reject', assignee: 'verifier', runtime: 'copaw-worker:223ddc2-agentloop-rag',
        started_at: null, ended_at: null,
        input_context: null,
        tool_calls: '无 — 从未被派发、从未执行',
        result_summary: null,
        result_status: null, effective: false, effective_note: 'LOCKED — fix-1 已拒绝，无修复可验证（零派发核验：PR3-RAG 窗口 @verifier mention = 0；无 pr3rag-verify-1 任务目录）',
        artifacts: [{ name: 'plan.md（[!] LOCKED 标注）', source: `${P}/project/plan.md` }],
        trace_id: null, trace_note: '未运行 — 无 trace',
        status_history_source: `${P}/project/plan.md + ${P}/team-room-messages-pr3rag-window.json`,
      },
    ],
    timeline,
    gate: {
      gate_id: 'elemiso-pr3rag-reject:post-review',
      state: 'rejected',
      trigger: {
        by: 'review-1',
        signals: ['FINDING_CONFIRMED', 'SEVERITY: HIGH', 'HUMAN_VERIFICATION_REQUIRED: YES'],
        triggered_at: `${D3}T17:25:54Z (exact, Leader 停门报告 $w3974fMdkFcPc…)`,
      },
      rejected_at: `${D3}T17:26:17Z (exact, DM $e9wIv3Qc2Dw3Yxt…)`,
      rejected_by: 'operator (runtime owner) — 运行前书面授权"PR #3 走拒绝分支"，由系统按授权自动投递',
      decision: 'HUMAN_SECURITY_REJECTED',
      record_path: `shared/projects/elemiso-pr3rag-reject/human-gate-rejection.md（快照：${P}/project/human-gate-rejection.md）`,
      scope: [
        '确认 HIGH/CWE-78 结论',
        '拒绝修复：pr3rag-fix-1 永不派发',
        'pr3rag-verify-1 保持锁定（无修复可验证）',
        '项目置 blocked，不完成',
        '失败即停止并保留证据（fail-safe）',
      ],
      prohibitions: ['fix-1 派发', 'verify-1 派发', '自动修复', 'merge', 'push', 'close', 'reopen'],
      unblocks: [],
      terminal_note: '终态：rejected → blocked。禁止从 rejected 自动转为 fixing/verifying/completed；仅新的显式人工批准事件可恢复（本案例不添加）。',
      rejection_text_source: `${P}/project/human-gate-rejection.md`,
    },
    fix_comparison: null,
    context_events: [
      {
        timestamp: ts3(17, 25, 30), timestamp_precision: 'exact',
        summary: 'RAG 纪律展示：reviewer 在完成全部自主复现之后才查询组织标准库，并在团队房明示"references only"——知识增强与结论独立并存',
        source_ref: `${P}/rag/rag-tool-spans.jsonl`,
      },
      {
        timestamp: ts3(17, 26, 21), timestamp_precision: 'exact',
        summary: '门纪律对照（同日两轮）：PR #2 批准路径 Leader 于批准后 9 秒发出全新委派；本轮拒绝路径 Leader 于拒绝后 9 秒落实绑定效应——两条分支 Leader 均未越权',
        source_ref: `${P}/team-room-messages-pr3rag-window.json`,
      },
    ],
    evidence_integrity: {
      sha256_dirs: ['FINALS-ELEM-PR3-RAG-TRACED (SHA256SUMS 41 files)'],
      secret_scan_clean: true,
      secret_scan_source: '打包时精确密钥扫描 0 命中；证据包不含原始 docker 容器日志',
    },
    sources: {
      case_definition: `${D.finalsPr3RagTraced}/README.md`,
      kickoff: `${D.finalsPr3RagTraced}/kickoff-as-sent.txt`,
      timeline: `${D.finalsPr3RagTraced}/team-room-messages-pr3rag-window.json`,
      review_result: `${D.finalsPr3RagTraced}/tasks/pr3rag-review-1/result.md`,
      reviewer_result: `${D.finalsPr3RagTraced}/tasks/pr3rag-review-1/result.md`,
      gate_request: `${D.finalsPr3RagTraced}/leader-dm-messages-pr3rag-window.json`,
      rejection_record: `${D.finalsPr3RagTraced}/project/human-gate-rejection.md`,
      rag_audit: `${D.finalsPr3RagTraced}/rag/rag-tool-spans.jsonl`,
      plan_final_state: `${D.finalsPr3RagTraced}/project/plan.md`,
      usage: `${D.finalsPr3RagTraced}/usage-summary.json`,
    },
  };
}

// ---------------------------------------------------------------------------
// Provider assembly + integrity gate
// ---------------------------------------------------------------------------
let cache = null;

export function getReplayData({ refresh = false } = {}) {
  if (cache && !refresh) return cache;
  const pr2 = buildPR2Rag();
  const pr3 = buildPR3Rag();
  const integrity = integrityReport();
  const artifacts = artifactRegistry();

  // Replay integrity gate: every source_ref must point to an existing evidence file.
  const refs = [];
  for (const c of [pr2, pr3]) {
    refs.push(...c.timeline.map((e) => e.source_ref));
    refs.push(...Object.values(c.sources));
    if (c.gate && c.gate.record_path) refs.push(c.gate.approval_text_source ?? c.gate.rejection_text_source);
    for (const t of c.tasks) for (const a of t.artifacts || []) refs.push(a.source);
  }
  const missingRefs = assertSourcesExist(refs);

  cache = {
    provider: 'replay',
    banner: 'REPLAY — HISTORICAL VERIFIED RUN (AgentLoop-traced, RAG-integrated)',
    generated_at: new Date().toISOString(),
    cases: { [pr2.case_id]: pr2, [pr3.case_id]: pr3 },
    integrity,
    artifacts,
    replay_integrity: {
      events_pr2: pr2.timeline.length,
      events_pr3: pr3.timeline.length,
      source_refs_checked: refs.length,
      missing_source_refs: missingRefs,
      ok: missingRefs.length === 0,
      note: '所有回放事件均锚定真实证据文件（两包 SHA256SUMS 锁定）；时间精度以 exact/approx/windowed 如实标注',
    },
    redaction_note: '探针中的 TOP-SECRET-OUTSIDE-BASE / LEGIT-INSIDE-BASE 为仓库内置演示占位串（非凭据），按规则显示',
  };
  return cache;
}

export function replayAudit() {
  const { integrity } = getReplayData();
  return {
    components: [
      { component: 'AgentTeams Controller', status: 'VERIFIED', source: 'agentteams-embedded:223ddc2（elemiso-ctrl，Docker reconcile 模式）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/README.md §资源' },
      { component: 'CoPaw Runtime', status: 'VERIFIED', source: 'copaw-worker:223ddc2-agentloop-rag（image 443e73375932 = 223ddc2-build1 + OTel v2.2 延迟埋点 + RAG MCP 延迟 hook；构建文件 release/agentloop-copaw-image/）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/image/' },
      { component: 'Matrix (Tuwunel)', status: 'VERIFIED', source: 'elemiso-matrix:6167（全部 agent 通信 + 事件历史；导出 749/465 事件含全部 event_id）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/team-room-messages.json' },
      { component: 'MinIO', status: 'VERIFIED', source: 'elemiso-controller:9000（teams/elemiso-team/shared/ 任务与项目存储）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/tasks/' },
      { component: 'LLM Gateway (Higress)', status: 'VERIFIED', source: 'elemiso-controller:8080（deepseek-chat，key-auth consumer 路由）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/higress-gateway-log-final.log' },
      { component: 'RAG MCP（组织知识库）', status: 'VERIFIED', source: 'rag_retrieve 经 MCP stdio 注入 CoPawAgent；知识型语料（8 文档/12 chunk，无案例结论）；服务端审计 rag-tool-spans.jsonl 全量落账（含 8 次运行时调用）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/rag/' },
      { component: 'AgentLoop/OTel', status: 'VERIFIED', source: '两轮全程 span 直连 SLS 导出：PR2-RAG 会话 647 span/216 批次 0 失败（含 tool.rag_retrieve ×8、taskflow/projectflow、matrix send/receive、genai LLM）；容器内直连探针 4×HTTP 200（trace 样本入包）；控制台人工核验截图入相邻证据包', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/agentloop/span-summary.json' },
      { component: 'PR Auto Merge', status: 'DISABLED', source: '人工门禁令（merge/push/close/reopen 全禁）；本轮零 GitHub 写入（ls-remote 双向核验分支 SHA 不变）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/github-branches-after-rag.txt' },
      { component: 'Event Integrity', status: 'VERIFIED', source: '两个 RAG 证据包 SHA256SUMS 启动时实时重算；历史 P14 包完整性报告见 /api/health（integrityReport）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/SHA256SUMS' },
      { component: 'PolarDB RAG backend', status: 'NOT_IMPLEMENTED', source: '真实 PolarDB 向量检索后端未接入；本轮 RAG 为本地知识型 SYNTHETIC 语料（data_mode=SYNTHETIC，契约与平台 /api/rag/search 一致）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/rag/rag-live-corpus.json' },
      { component: 'Agentic Database Branch', status: 'NOT_IMPLEMENTED', source: '真实 PolarDB Branch 未实现（MCP server 中的 database_* 工具未被本轮运行调用）', evidence: 'FINALS-ELEM-PR2-RAG-TRACED/rag/rag-tool-spans.jsonl（无 database_* 记录）' },
    ],
    residual_risks: [
      { item: 'Reviewer 跨轮记忆（如实披露）', detail: 'worker 家目录由 MinIO 跨轮持久同步，reviewer 团队房可见"Code confirmed identical to prior runs"；两轮结论仍由其自主 clone/复测得出；RAG 语料不含任何案例结论（knowledge-only 设计即为保护独立性）。', source: 'FINALS-ELEM-PR2-RAG-TRACED/README.md §RAG 使用实录' },
      { item: 'RAG 为 citation-only + SYNTHETIC', detail: 'rag_retrieve 返回引用（document_id/chunk_id/score/source_ref）不含正文；语料为合成组织规范（非企业数据）；服务端只保存 query_hash。', source: 'FINALS-ELEM-PR2-RAG-TRACED/rag/rag-live-corpus.json (_doc)' },
      { item: '本轮 LLM 缓存命中率 94%（前轮 98%）', detail: 'worker 容器全新冷启动导致 prompt cache 部分失效；用量如实记录（PR2-RAG 80 调用/6.65M 输入/24.6K 输出；PR3-RAG 31 调用/2.87M/9.2K）。', source: 'FINALS-ELEM-PR2-RAG-TRACED/usage-summary.json' },
      { item: 'controller Stopped 语义移除容器', detail: '运行结束 CR Stopped 后 controller 移除 worker 容器与 auth 卷（CR 与 ctrl 数据卷保留）；全部容器内证据于停机前实时采集（span/audit/探针）。', source: 'FINALS-ELEM-PR2-RAG-TRACED/README.md §采集顺序' },
      { item: 'PolarDB RAG backend：未接入', detail: '未接通 PolarDB 作为向量/知识检索后端；RAG 为本地知识型 SYNTHETIC 语料（zero-dep lexical 检索）。', source: 'FINALS-ELEM-PR2-RAG-TRACED/rag/rag-live-server.mjs' },
      { item: 'Agentic Database Branch：未实现', detail: '无真实数据库写时分支隔离；MCP server 暴露的 database_* 工具本轮零调用。', source: 'FINALS-ELEM-PR2-RAG-TRACED/rag/rag-tool-spans.jsonl' },
    ],
    stability_events: [
      { item: 'RAG 接入调试如实记录', detail: 'MCP 注入曾两处失败（StdIOStatefulClient 需显式 connect()；cwd="" 致 spawn FileNotFoundError），均在隔离容器内定位修复并以 canary/端到端 MCP 握手验证后才进入正式运行；修复版构建文件与验证记录已入包。', source: 'FINALS-ELEM-PR2-RAG-TRACED/image/ + rag/rag-tool-spans.jsonl' },
      { item: '门决策执行模式', detail: '本轮两门按操作员运行前书面授权自动执行（PR #2=批准、PR #3=拒绝），授权范围与先例一致（零 GitHub 写入）；门记录均先于派发/终报落盘项目存储。', source: 'FINALS-ELEM-PR2-RAG-TRACED/project/human-gate-approval.md' },
    ],
    sha256sums: integrity,
    secret_scan: {
      clean: true,
      source: '打包时精确密钥扫描（license key、admin/minio 密码、gateway key、matrix token 反向比对）',
      detail: '0 命中；证据包不含原始 docker 容器日志（防凭据片段）；AgentLoop license key 仅经 stdin 注入容器开关文件，未入镜像/Git/证据包',
    },
    verdicts: [
      { phase: 'PR2-RAG-TRACED', verdict: 'REAL_EXECUTED + REAL_TRACED + REAL_RAG_MCP_INTEGRATION（completed，墙钟 4m15s）', source: 'FINALS-ELEM-PR2-RAG-TRACED/README.md' },
      { phase: 'PR3-RAG-TRACED', verdict: 'PROJECT_BLOCKED_HUMAN_REJECTED（零派发三重核验，墙钟 1m19s）', source: 'FINALS-ELEM-PR3-RAG-TRACED/README.md' },
    ],
  };
}
