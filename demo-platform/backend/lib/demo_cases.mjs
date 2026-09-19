// backend/lib/demo_cases.mjs — finals demo case adapter (v2: real-PR-led).
//
// Case A  fastapi-pr2-cwe22   — nghqqa/fastapi-boilerplate-demo PR #2 (CWE-22
//                               path traversal), replayed from the locked
//                               Phase-14 AgentTeams evidence. HISTORICAL_REPLAY
//                               of a REAL_EXECUTED-AgentTeams run; PR stays OPEN;
//                               no GitHub writes.
// Case B  rework-payments     — payment-idempotency REWORK loop. This is a
//                               CONTROL_PLANE_MECHANISM case: real controller +
//                               real PG audit chain + real tests, but reviewer
//                               findings / patches are controlled input and the
//                               repo/PR are NOT externally verifiable — the UI
//                               must never present it as a real external PR.
// Case C  db-migration-orders — LOCAL_REAL_SQL loop, kept as ADDITIONAL evidence
//                               only (excluded from the selector and overview).
//
// Honesty contract: fixed six-label vocabulary; missing fields render as
// 未提供/未执行; loaders return { available:false } instead of inventing data.

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readText, readJson, exists, resolveSourceRef } from '../../evidence-adapter/evidence.mjs';
import { getReplayData } from '../../evidence-adapter/replay-provider.mjs';
import { reworkLoopSummary, dbLoopSummary, finalsIntegrity } from './finals_evidence.mjs';

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

// Fixed evidence-level vocabulary (UI must never invent other levels).
export const EVIDENCE_LEVELS = {
  REAL_EXECUTED_AGENTTEAMS_LIVE: '真实运行 · AgentTeams',
  REAL_EXECUTED_AGENTTEAMS: '真实执行 · AgentTeams（历史运行）',
  REAL_OFFLINE_EXPERIMENT: '真实离线实验 · 合成语料',
  POST_RUN_DESIGN: '运行后固化设计 · 非运行时证据',
  LOCAL_REAL_SQL: '本地真实 SQL（隔离 PostgreSQL）',
  CONTROL_PLANE_MECHANISM: '控制面机制验证',
  SYNTHETIC: '合成数据',
  HISTORICAL_REPLAY: '历史回放',
  NOT_EXECUTED: '未执行',
};

const short = (s, n = 12) => (s ? `${String(s).slice(0, n)}…` : '—');

// Read a repo file outside the evidence dirs (rework_case/base, read-only).
function repoText(rel) {
  try {
    return readText; // placeholder to satisfy linters never hit
  } catch { /* noop */ }
}

function readRepoFile(rel) {
  const fs = requireFs();
  try {
    return fs.readFileSync(path.join(REPO_ROOT, rel), 'utf8');
  } catch {
    return null;
  }
}
import { createRequire } from 'node:module';
const requireFs = () => createRequire(import.meta.url)('node:fs');

// Honesty triples per case.
const PR2_HONESTY = {
  real: ['AgentTeams/CoPaw 多 Agent 历史真实运行（2026-08-29：真实 Matrix 派单、真实 LLM、真实容器执行）', 'Reviewer / Fixer / Verifier 产物来自锁定证据目录（SHA256 校验）', '人工审批为真实操作员记录（human-gate-approval.md）'],
  controlled: ['本页面对证据的时间线投影与讲解文案'],
  not_executed: ['本次演示不连接实时 GitHub / Matrix / LLM', 'merge / push / close / reopen 全程禁止，PR #2 保持 OPEN', 'per-task 云端 Trace（evidence-replay，trace_id 不存在）'],
};
const REWORK_HONESTY = {
  real: ['workflow-controller 控制器代码真实执行', 'PostgreSQL 审计链真实写入', '验收测试真实执行并决定 VERDICT'],
  controlled: ['Reviewer 结论（reviewer_findings.json）', 'Fixer 两个补丁（patches/*.diff）'],
  not_executed: ['LLM 调用', 'Matrix/Element 真实交接', 'GitHub 写入', 'CoPaw 容器运行'],
};
const DB_HONESTY = {
  real: ['隔离 PostgreSQL 16.14 clone 上真实执行迁移与断言 SQL', '审批/绑定/gate 记录真实写入审计库', '政策网关 wrapper 以模块导入方式真实执行'],
  controlled: ['案例数据（orders-demo 基线与种子为 SYNTHETIC 合成数据）'],
  not_executed: ['PolarDB 连接（NOT CONNECTED）', 'Agentic Database Branch', 'GitHub 写入', '生产发布（NOT PERFORMED）'],
};

let cache = null;
function build() {
  if (cache) cache = null; // v2: recompute each process start only
  if (!cache) {
    cache = {
      replay: getReplayData(),
      rework: reworkLoopSummary(),
      db: dbLoopSummary(),
      integrity: finalsIntegrity(),
    };
  }
  return cache;
}

// ------------------------------------------------------------- case A: PR #2

function probeTexts() {
  const out = {};
  for (const f of ['artifacts/verify-1.before_probe_raw.txt', 'artifacts/verify-1.after_probe_raw.txt']) {
    out[f] = exists('fixVerify', f) ? readText('fixVerify', f) : null;
  }
  return out;
}

function refHash(dirKey, file) {
  const r = resolveSourceRef(`${dirKey}/${file}`);
  return r ? { file: r.file, dir: r.dir, exists: r.exists, hash_verified: r.hash_verified ?? null, sha256: r.sha256_actual ?? null } : null;
}

function caseAFastAPI() {
  const b = build();
  const c = b.replay.cases['pr2-high-risk-human-gate'];
  if (!c) return null;
  const prCreated = exists('highRiskGateBlocked', 'pr-created.json') ? readJson('highRiskGateBlocked', 'pr-created.json') : null;
  const pr = prCreated?.pr ?? null;
  const patch = exists('replayMaterials', 'artifacts/fix.patch') ? readText('replayMaterials', 'artifacts/fix.patch') : null;
  const approval = exists('replayMaterials', 'artifacts/human-gate-approval.md') ? readText('replayMaterials', 'artifacts/human-gate-approval.md') : null;
  const probes = probeTexts();
  const verifyReport = exists('fixVerify', 'artifacts/verify-1.verification-report.md') ? readText('fixVerify', 'artifacts/verify-1.verification-report.md') : null;
  const verifyResult = exists('fixVerify', 'artifacts/verify-1.result.md') ? readText('fixVerify', 'artifacts/verify-1.result.md') : null;
  const fixResult = exists('fixVerify', 'artifacts/fix-1.result.md') ? readText('fixVerify', 'artifacts/fix-1.result.md') : null;
  const fixTestEvidence = exists('fixVerify', 'artifacts/fix-1.test-evidence.md') ? readText('fixVerify', 'artifacts/fix-1.test-evidence.md') : null;
  const prState = exists('finalLock', 'pr-state-final.json') ? readJson('finalLock', 'pr-state-final.json') : null;

  const commitSha = pr?.commit_sha ?? null;
  const attackReq = 'GET /demo/download?name=../outside/outside-secret.txt';
  const legitReq = 'GET /demo/download?name=welcome.txt';

  const items = [
    {
      id: 'ev-pr-created', title: 'PR 创建记录 pr-created.json', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['仓库', pr?.repository ?? c.repository],
        ['PR', `#${pr?.number ?? 2} · ${pr?.title ?? '未提供'}`],
        ['URL', pr?.url ?? '未提供'],
        ['base / head', `${pr?.base ?? '未提供'} ← ${pr?.head ?? c.pull_request.branch}`],
        ['commit_sha', commitSha ?? '未提供'],
        ['变更规模', `${pr?.changed_files ?? '—'} files · +${pr?.additions ?? '—'} / -${pr?.deletions ?? '—'}`],
      ],
      blocks: [{ title: 'highRiskGateBlocked/pr-created.json', lang: 'json', text: prCreated ? JSON.stringify(prCreated, null, 2) : '未提供' }],
      source_ref: 'highRiskGateBlocked/pr-created.json',
      hash: refHash('highRiskGateBlocked', 'pr-created.json'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-case-definition', title: '案例定义 02-case-pr2-high-risk.md', level: 'HISTORICAL_REPLAY',
      fields: [
        ['PR 状态', '全程 OPEN — 未 merge/push/close/reopen'],
        ['风险', 'CWE-22 路径穿越 / 任意文件读取（演示预置缺陷）'],
        ['受影响文件', c.risk.affected_file],
      ],
      blocks: [{ title: 'replayMaterials/02-case-pr2-high-risk.md', lang: 'text', text: exists('replayMaterials', '02-case-pr2-high-risk.md') ? readText('replayMaterials', '02-case-pr2-high-risk.md') : '未提供' }],
      source_ref: 'replayMaterials/02-case-pr2-high-risk.md',
      hash: refHash('replayMaterials', '02-case-pr2-high-risk.md'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-review-conclusion', title: 'Reviewer 结论（review-1）', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['协议标记', c.risk.reviewer_conclusion],
        ['风险', `${c.risk.category} · severity=HIGH · 任意文件读取`],
        ['位置', c.risk.affected_file],
        ['是否需要人工门', 'HUMAN_VERIFICATION_REQUIRED: YES → 触发人工安全门'],
        ['执行者', 'p14h2-copaw-worker-reviewer（真实历史运行）'],
      ],
      blocks: [],
      source_ref: 'replayMaterials/02-case-pr2-high-risk.md §1 + fixAudit/AUDIT.md',
      ...PR2_HONESTY,
    },
    {
      id: 'ev-probe-before', title: '修复前探针原始输出（漏洞确认）', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['穿越请求', `${attackReq} → HTTP 200`],
        ['泄露', "TOP-SECRET-OUTSIDE-BASE · LEAKED_OUTSIDE_SECRET: True"],
        ['合法请求', `${legitReq} → HTTP 200`],
      ],
      blocks: [{ title: 'fixVerify/artifacts/verify-1.before_probe_raw.txt', lang: 'log', text: probes['artifacts/verify-1.before_probe_raw.txt'] ?? '未提供' }],
      source_ref: 'fixVerify/artifacts/verify-1.before_probe_raw.txt',
      hash: refHash('fixVerify', 'artifacts/verify-1.before_probe_raw.txt'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-fix-patch', title: 'Fixer 最小修复 fix.patch', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['文件', 'backend/src/interfaces/api/v1/demo_high_risk.py'],
        ['规模', '单文件 · 12 insertions / 4 deletions'],
        ['策略', 'Path.resolve() 归一化 + is_relative_to(DEMO_FILES_DIR) 包含性校验；越界/不存在 → 404'],
        ['blob 锚点', 'index 59cd8d1..b3d3b46（fix.patch git index 行）'],
        ['写入 GitHub', '否 — 补丁仅以交付物形态存在（NOT_EXECUTED）'],
      ],
      blocks: [{ title: 'replayMaterials/artifacts/fix.patch', lang: 'diff', text: patch ?? '未提供' }],
      source_ref: 'replayMaterials/artifacts/fix.patch',
      hash: refHash('replayMaterials', 'artifacts/fix.patch'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-fix-result', title: 'Fixer 交付 fix-1.result.md', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['STATUS', 'SUCCESS / FIX_APPLIED / TESTS_PASSED'],
        ['交付物', 'fix.patch · demo_high_risk.fixed.py · test-evidence.md'],
        ['Leader 验收', 'effective=true（fixVerify/AUDIT.md）'],
      ],
      blocks: [{ title: 'fixVerify/artifacts/fix-1.result.md', lang: 'text', text: fixResult ?? '未提供' }],
      source_ref: 'fixVerify/artifacts/fix-1.result.md',
      hash: refHash('fixVerify', 'artifacts/fix-1.result.md'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-fix-test-evidence', title: 'Fixer 本地测试说明 fix-1.test-evidence.md', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['说明', '仓库自带漏洞断言测试在修复后转失败 — 断言的是修复前 200/泄露行为（预期翻转，非回归）'],
        ['模块导入冒烟', '通过'],
      ],
      blocks: [{ title: 'fixVerify/artifacts/fix-1.test-evidence.md', lang: 'text', text: fixTestEvidence ?? '未提供' }],
      source_ref: 'fixVerify/artifacts/fix-1.test-evidence.md',
      hash: refHash('fixVerify', 'artifacts/fix-1.test-evidence.md'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-probe-after', title: '修复后探针原始输出（Verifier 独立复测）', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['穿越请求', `${attackReq} → HTTP 404 · 无泄露`],
        ['合法请求', `${legitReq} → HTTP 200 仍可用`],
        ['TRAVERSAL_REJECTED', 'True'],
      ],
      blocks: [{ title: 'fixVerify/artifacts/verify-1.after_probe_raw.txt', lang: 'log', text: probes['artifacts/verify-1.after_probe_raw.txt'] ?? '未提供' }],
      source_ref: 'fixVerify/artifacts/verify-1.after_probe_raw.txt',
      hash: refHash('fixVerify', 'artifacts/verify-1.after_probe_raw.txt'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-verify-report', title: 'Verifier 独立验证报告 verification-report.md', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['独立工作区', '重 clone → git apply fix.patch（字节级一致）→ 自设计探针 → 回归检查'],
        ['角色区分', 'Verifier 不信任 Fixer 结论，独立复现验证'],
      ],
      blocks: [{ title: 'fixVerify/artifacts/verify-1.verification-report.md', lang: 'text', text: verifyReport ?? '未提供' }],
      source_ref: 'fixVerify/artifacts/verify-1.verification-report.md',
      hash: refHash('fixVerify', 'artifacts/verify-1.verification-report.md'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-verify-result', title: 'Verifier 结果 verify-1.result.md', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['STATUS', 'VERIFICATION_PASSED / SEVERITY: NONE / HUMAN_VERIFICATION_REQUIRED: NO'],
        ['标记', 'FIX_INDEPENDENTLY_VERIFIED · REGRESSION_CHECK_PASSED'],
        ['披露', 'VERIFICATION_PASSED 字面值不在 store 白名单（枚举兼容问题，如实披露）'],
      ],
      blocks: [{ title: 'fixVerify/artifacts/verify-1.result.md', lang: 'text', text: verifyResult ?? '未提供' }],
      source_ref: 'fixVerify/artifacts/verify-1.result.md',
      hash: refHash('fixVerify', 'artifacts/verify-1.result.md'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-gate-approval', title: '人工安全门审批记录 human-gate-approval.md', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['决定', 'APPROVED（2026-08-29，操作员经受保护控制台）'],
        ['范围', '确认 HIGH_RISK_FOUND → 授权 fix-1 → 完成后授权 verify-1'],
        ['禁止', 'merge / push / close / reopen'],
      ],
      blocks: [{ title: 'replayMaterials/artifacts/human-gate-approval.md', lang: 'text', text: approval ?? '未提供' }],
      source_ref: 'replayMaterials/artifacts/human-gate-approval.md',
      hash: refHash('replayMaterials', 'artifacts/human-gate-approval.md'),
      ...PR2_HONESTY,
    },
    {
      id: 'ev-pr-write-audit', title: 'PR 写操作审计（零写入证明）', level: 'HISTORICAL_REPLAY',
      fields: [
        ['本 runtime 的 PR 操作', prState?.operations_by_this_runtime ?? 'none'],
        ['PR #2 状态', 'OPEN（保持未合并）'],
        ['live 检查', prState?.live_api_check ?? '未提供'],
      ],
      blocks: [{ title: 'finalLock/pr-state-final.json', lang: 'json', text: prState ? JSON.stringify(prState, null, 2) : '未提供' }],
      source_ref: 'finalLock/pr-state-final.json + highRiskGateBlocked/pr-write-audit.json',
      hash: refHash('finalLock', 'pr-state-final.json'),
      ...PR2_HONESTY,
    },
  ];

  const timelineEvents = (needle) => (c.timeline || []).filter((e) => e.summary.includes(needle));

  const steps = [
    {
      id: 'pr-init', title: 'PR 发起',
      points: [
        { k: '仓库 / PR', v: `${pr?.repository ?? c.repository} · PR #${pr?.number ?? 2} · ${pr?.title ?? '未提供'}` },
        { k: '分支', v: `base ${pr?.base ?? '未提供'} ← head ${pr?.head ?? c.pull_request.branch}`, mono: true },
        { k: 'commit SHA', v: commitSha ?? '未提供', mono: true },
        { k: '变更目标', v: '新增演示路由 demo_high_risk.py（预置漏洞）+ 路径穿越复现单测，2 files · +122' },
        { k: '风险预期', v: 'PR 描述声明“高危安全演示：修复前必须人工确认”——预期触发人工安全门' },
      ],
      evidence: ['ev-pr-created', 'ev-case-definition'],
      probe: null,
      detail: {
        title: '运行环境',
        quote: `project copaw-high-risk-human-gate · runtime ${c.runtime.worker_image} · 团队 ${c.team.id}`,
        outbox: [], events: timelineEvents('PROJECT COMPLETED').slice(0, 1),
      },
    },
    {
      id: 'review', title: '风险审查',
      points: [
        { k: 'Reviewer 结论', v: c.risk.reviewer_conclusion },
        { k: '风险定性', v: 'CWE-22 路径穿越 / 任意文件读取 —— name 参数直接 os.path.join 到 DEMO_FILES_DIR，../ 可逃逸基目录', },
        { k: '受影响位置', v: c.risk.affected_file, mono: true },
        { k: '攻击请求', v: `${attackReq} → HTTP 200 · 泄露 TOP-SECRET-OUTSIDE-BASE`, mono: true },
        { k: '严重级别 / 人工门', v: 'SEVERITY: HIGH · HUMAN_VERIFICATION_REQUIRED: YES → 全系统暂停等人工' },
      ],
      evidence: ['ev-review-conclusion', 'ev-probe-before'],
      probe: null,
      attack: { request: attackReq, legit: legitReq },
      detail: {
        title: '派单与执行细节',
        quote: 'review-1 由真实 Matrix 消息派发（event_id 记录于审计），聚焦 demo_download 路径处理，输出协议化结论。',
        outbox: [], events: timelineEvents('HIGH_RISK_FOUND').slice(0, 1).concat(timelineEvents('HUMAN_SECURITY_REVIEW_REQUIRED').slice(0, 1)),
      },
    },
    {
      id: 'fix-diff', title: '修复 diff',
      points: [
        { k: '交付物', v: 'fix.patch — 单文件最小修复（12+/4−），未写入 GitHub', },
        { k: '文件路径', v: 'backend/src/interfaces/api/v1/demo_high_risk.py', mono: true },
        { k: '修复策略', v: 'Path.resolve() 归一化 + is_relative_to(DEMO_FILES_DIR) 包含性校验；越界/不存在 → 404；filename 用 target.name 防头部注入' },
        { k: 'patch 校验', v: 'git index 59cd8d1..b3d3b46 · 文件在锁定证据包内 SHA256 校验通过', mono: true },
        { k: '边界', v: '补丁以交付物形态存在 —— 是否进入远端分支属仓库维护者的人工决策' },
      ],
      evidence: ['ev-fix-patch', 'ev-fix-result', 'ev-fix-test-evidence'],
      probe: null,
      detail: {
        title: 'fixer 执行细节',
        quote: 'fixer 本地 clone 分支 demo/high-risk-human-gate（无远端凭证，仅本地作业）→ 修复前探针 → 最小修复 → 修复后探针 404/200。',
        outbox: [], events: timelineEvents('fix-1 正式提交').slice(0, 1),
      },
    },
    {
      id: 'verify', title: '独立验证',
      points: [
        { k: '独立工作区', v: 'Verifier 重 clone → git apply fix.patch（字节级一致）→ 自设计探针 → 回归检查', },
        { k: '测试命令', v: '自设计 HTTP 探针 probe.py（穿越 + 合法双请求）+ pytest 回归 + 模块导入冒烟', mono: true },
        { k: '修复前后', v: `穿越请求 200（泄露）→ 404（拒绝）；合法文件 200 → 200 仍可用`, mono: false },
        { k: '结论', v: 'STATUS: VERIFICATION_PASSED · SEVERITY: NONE · FIX_INDEPENDENTLY_VERIFIED · REGRESSION_CHECK_PASSED' },
        { k: '角色区分', v: 'Reviewer 发现风险；Verifier 独立复测且不信任 Fixer 自述 —— 两个角色、两份独立证据' },
      ],
      evidence: ['ev-verify-report', 'ev-probe-after', 'ev-verify-result'],
      probe: {
        headline: '探针对照 · 越权读取关闭，合法访问保留',
        rows: [
          { label: '越权读取（路径穿越）', request: attackReq, before: 'HTTP 200 · 泄露 TOP-SECRET', after: 'HTTP 404 · 拒绝', kind: 'fixed' },
          { label: '合法文件 welcome.txt', request: legitReq, before: 'HTTP 200', after: 'HTTP 200 · 仍可用', kind: 'stable' },
        ],
      },
      detail: {
        title: '验证事件链',
        quote: 'verify-1 于 11:06:25Z 实时消费派单（精确锚点），11:26Z 提交 VERIFICATION_PASSED。',
        outbox: [], events: timelineEvents('verify-1 提交').slice(0, 1),
      },
    },
    {
      id: 'approval', title: '人工审批 · 最终处置',
      points: [
        { k: '人工审批', v: '已执行 —— 2026-08-29 操作员经受保护控制台批准（批准记录 human-gate-approval.md，SHA256 校验）' },
        { k: '验证结论', v: 'VERIFICATION_PASSED · SEVERITY: NONE（独立验证）' },
        { k: 'PR 状态', v: 'PR #2 保持 OPEN —— merge/push/close/reopen 全程禁止且未发生' },
        { k: '写入 GitHub', v: '未执行 —— 本次演示不写入 GitHub（NOT_EXECUTED）' },
      ],
      evidence: ['ev-gate-approval', 'ev-pr-write-audit'],
      probe: null,
      decision: {
        verified: { verdict: 'PASS', text: 'VERIFICATION_PASSED —— 越权 200→404，合法 200→200（Verifier 独立复测）' },
        human_approval: { needed: true, text: '需要且已执行 —— 真实操作员审批记录在案（human-gate-approval.md）', level: 'REAL_EXECUTED_AGENTTEAMS' },
        merge_allowed: { allowed: false, text: 'PR #2 保持 OPEN —— 合并权限未在本演示中执行（merge/push/close/reopen 被审批记录明令禁止）' },
        github_write: { done: false, text: '未写入 —— 本次演示不写入 GitHub', level: 'NOT_EXECUTED' },
        execution_nature: { text: 'HISTORICAL_REPLAY · REAL_EXECUTED-AgentTeams：回放 2026-08-29 的真实历史运行证据；本次演示本身不执行任何 Agent', level: 'HISTORICAL_REPLAY' },
        pr_open: true,
      },
      detail: {
        title: '终态事件',
        outbox: [], events: timelineEvents('PROJECT COMPLETED').slice(0, 1),
      },
    },
  ];

  return {
    case_id: 'fastapi-pr2-cwe22',
    shape: 'guided',
    name: 'FastAPI PR #2 · 路径穿越风险修复',
    short_name: '主案例 · FastAPI PR #2',
    one_liner: '真实历史 PR：Reviewer 确认 CWE-22 任意文件读取 → 人工门批准 → Fixer 最小修复 → Verifier 独立复测 200→404，PR 保持 OPEN。',
    repo: pr?.repository ?? 'nghqqa/fastapi-boilerplate-demo',
    pr: `PR #${pr?.number ?? 2} · ${pr?.head ?? 'demo/high-risk-human-gate'}`,
    pr_url: pr?.url ?? null,
    run_id: 'copaw-high-risk-human-gate（AgentTeams project）',
    sha: commitSha,
    sha_kind: 'PR #2 head commit',
    evidence_level: ['HISTORICAL_REPLAY', 'REAL_EXECUTED_AGENTTEAMS'],
    replay_note: 'HISTORICAL_REPLAY —— 本页面回放 2026-08-29 的历史运行证据；不连接实时 GitHub / Matrix / 模型',
    purpose: '展示真实 PR 上的完整审修闭环：高危发现 → 人工门 → 最小修复 → 独立验证',
    risk_tags: ['CWE-22 路径穿越', '任意文件读取', 'HIGH'],
    status: { verdict: 'PASS', label: '修复已独立验证 · PR 保持 OPEN' },
    facts: {
      real_github_pr: { value: '真实 GitHub PR（历史状态：OPEN）', ok: true, note: pr?.url ?? '' },
      real_agentteams_run: { value: '真实 AgentTeams 运行（2026-08-29 历史证据）', ok: true, note: '真实 Matrix 派单 / LLM / 容器执行' },
      github_write: { value: '未写入 —— PR 保持 OPEN', ok: false, note: 'merge/push/close/reopen 全程禁止' },
    },
    banner: '历史回放 —— 本案例是 2026-08-29 的历史真实运行对照；最新证据见主案例「本次真实运行（2026-09-16）」',
    stage_timeline: null,
    relation_note: '与主案例 fastapi-pr2-live-20260916 为同一 PR：本卡是 2026-08-29 历史运行对照（当时亦为真实运行，今日已复跑）。',
    portfolio_role: '案例 A 对照 · 历史回放',
    chain: ['案例 fastapi-pr2-cwe22', 'PR #2', `commit ${short(commitSha, 10)}`, 'demo_high_risk.py', 'fix.patch', 'probe 200→404', 'VERIFICATION_PASSED'],
    generated_at: '运行 2026-08-29 · 状态锁定 2026-08-29T13:09+08:00（pr-state-final.json）',
    source_dir: 'evidence/PHASE14-WINDOWS-*（replayMaterials · fixVerify · fixAudit · finalLock · highRiskGateBlocked）',
    honesty: PR2_HONESTY,
    steps,
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// --------------------------------------------------- case D: PR #2 live run (2026-09-16)

const PR2_LIVE_HONESTY = {
  real: ['本次真实执行（2026-09-16）：真实 Matrix 派单、真实容器内 clone/测试，全部 event_id 可审计；Reviewer 复现与定级独立', '漏洞类别与目标文件由 kickoff 指令点名（非盲测）；人工门为操作员真实批准（修复前）', 'Verifier 在独立干净工作区验证，patch sha256 独立复现一致；请求模型 deepseek-chat（网关记录响应 model=deepseek-flash）'],
  controlled: ['本页面为证据的只读回放与讲解文案'],
  not_executed: ['本次演示不连接实时 GitHub / Matrix / LLM', 'merge / push / close / reopen 全程禁止，PR #2 保持 OPEN', 'GitHub 写入未执行（NOT_EXECUTED）'],
};

function caseDPr2Live() {
  if (!exists('finalsPr2Live', 'README.md')) return null;
  const reviewerResult = exists('finalsPr2Live', 'reviewer-result.md') ? readText('finalsPr2Live', 'reviewer-result.md') : null;
  const fixerResult = exists('finalsPr2Live', 'fixer-result.md') ? readText('finalsPr2Live', 'fixer-result.md') : null;
  const fixerDiff = exists('finalsPr2Live', 'fixer-attempt-1.diff') ? readText('finalsPr2Live', 'fixer-attempt-1.diff') : null;
  const verifierResult = exists('finalsPr2Live', 'verifier-result.md') ? readText('finalsPr2Live', 'verifier-result.md') : null;
  const verification = exists('finalsPr2Live', 'verifier-workspace/verification.md') ? readText('finalsPr2Live', 'verifier-workspace/verification.md') : null;
  const gateApproval = exists('finalsPr2Live', 'human-gate-approval.md') ? readText('finalsPr2Live', 'human-gate-approval.md') : null;
  const audit = exists('finalsPr2Live', 'AUDIT.md') ? readText('finalsPr2Live', 'AUDIT.md') : null;
  const reviewerRepro = exists('finalsPr2Live', 'reviewer-pr2-review-repro.py') ? readText('finalsPr2Live', 'reviewer-pr2-review-repro.py') : null;
  const teamRoom = exists('finalsPr2Live', 'team-room-messages.json') ? readJson('finalsPr2Live', 'team-room-messages.json') : null;
  const leaderDm = exists('finalsPr2Live', 'leader-dm-messages.json') ? readJson('finalsPr2Live', 'leader-dm-messages.json') : null;
  const meta = exists('finalsPr2Live', 'PR-METADATA.md') ? readText('finalsPr2Live', 'PR-METADATA.md') : null;
  const roleContractsD = ['ROLE-LEADER.md','ROLE-REVIEWER.md','ROLE-FIXER.md','ROLE-VERIFIER.md'].map((rf) => ({ name: rf, text: readRepoFile('tools/agentteams/roles/' + rf) }));

  const RUN = 'run-elem-fastapi-pr2-20260916-01';
  const SHA = '1dedf5e1992c950557064d8f4fb9039d1523deb3';
  const attack = 'GET /demo/download?name=../outside/outside-secret.txt';

  const items = [
    {
      id: 'ev-live-meta', title: '任务元数据 PR-METADATA.md（下发全部角色）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['run_id / project', `${RUN} · elemiso-pr2-gate（team elemiso-team）`],
        ['head SHA', SHA, 'mono'],
        ['编排', 'CoPaw Leader（projectflow/taskflow/delegate_task/check_task）· 3 copaw worker'],
        ['镜像', 'agentteams-embedded / copaw-worker:223ddc2-build1（ae47995d209f / cdc8f8a4ab8d，与 P14 证据一致）'],
      ],
      blocks: [{ title: 'finalsPr2Live/PR-METADATA.md', lang: 'text', text: meta ?? '未提供' }],
      source_ref: 'finalsPr2Live/PR-METADATA.md',
      hash: refHash('finalsPr2Live', 'PR-METADATA.md'),
      ...PR2_LIVE_HONESTY,
    },
    {
      id: "ev-live-roles", title: "角色契约（v1.0 冻结，跨案例零改动）", level: 'POST_RUN_DESIGN',
      fields: [
        ['契约版本', 'v1.0（冻结，跨案例零改动；回溯固化自 P14 角色矩阵与两轮真实运行的公共协议）'],
        ['要点', '角色行为与案例参数分离：Reviewer/Fixer/Verifier/Leader 契约在两个 PR 的运行中逐字相同，案例差异只在 CASE-MANIFEST（SHA/目标文件/task id/验收行为）'],
      ],
      blocks: roleContractsD.map((rc) => ({ title: 'tools/agentteams/roles/' + rc.name, lang: 'markdown', text: rc.text ?? '未提供' })),
      source_ref: 'tools/agentteams/roles/（仓库文件，非案例工件）',
      ...PR2_LIVE_HONESTY,
    },
    {
      id: 'ev-live-review', title: 'Reviewer 结论 pr2-review-1（真实执行）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['结论', 'STATUS: SUCCESS · FINDING_CONFIRMED · SEVERITY: HIGH · HUMAN_VERIFICATION_REQUIRED: YES'],
        ['定性', 'CWE-22 路径穿越/任意文件读取（demo_download L41 os.path.join → L42 FileResponse）'],
        ['真实复现', '仓库复现测试 1 passed（越基目录读文件 HTTP 200）；Reviewer 自写 PoC 以 ../../../etc/hostname 读到容器内真实文件'],
        ['事件', '委派 $lK5NAkgCQwxnNIoTahtvwFSQW4J_WrNqx5RyM24wNBI'],
      ],
      blocks: [
        { title: 'finalsPr2Live/reviewer-result.md', lang: 'text', text: reviewerResult ?? '未提供' },
        { title: 'finalsPr2Live/reviewer-pr2-review-repro.py（Reviewer 自写 PoC）', lang: 'python', text: reviewerRepro ?? '未提供' },
      ],
      source_ref: 'finalsPr2Live/reviewer-result.md',
      hash: refHash('finalsPr2Live', 'reviewer-result.md'),
      ...PR2_LIVE_HONESTY,
    },
    {
      id: 'ev-live-gate', title: '人工安全门批准（本次真实批准，修复前）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['决策', '操作员在查看具体 findings 后批准修复+验证（不复用任何历史批准）'],
        ['位置', '门保持在 Reviewer 之后、Fixer 之前 —— 与 P14 历史一致'],
        ['附带处置', 'Leader 曾在门前违规委派 → 已作废并回滚；Fixer 曾未委派自启 → 已叫停（如实披露，见 AUDIT §3）'],
      ],
      blocks: [{ title: 'finalsPr2Live/human-gate-approval.md', lang: 'text', text: gateApproval ?? '未提供' }],
      source_ref: 'finalsPr2Live/human-gate-approval.md',
      hash: refHash('finalsPr2Live', 'human-gate-approval.md'),
      ...PR2_LIVE_HONESTY,
    },
    {
      id: 'ev-live-fix', title: 'Fixer 补丁 pr2-fix-1 attempt-1（真实执行）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['交付', 'attempt-1.diff 单文件 +13/−7（仅 demo_high_risk.py，测试零改动）'],
        ['策略', 'os.path.realpath 归一化 + os.path.commonpath 包含性校验；越界 400、缺失 404、合法 200'],
        ['patch sha256', '674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081', 'mono'],
        ['派发链(审计修正)', '门后无新委派事件(Matrix 事务号幂等复用被作废事件);Fixer 由操作员按 SPEC B 直接指令开工,如实披露'],
      ],
      blocks: [
        { title: 'finalsPr2Live/fixer-attempt-1.diff', lang: 'diff', text: fixerDiff ?? '未提供' },
        { title: 'finalsPr2Live/fixer-result.md', lang: 'text', text: fixerResult ?? '未提供' },
      ],
      source_ref: 'finalsPr2Live/fixer-attempt-1.diff',
      hash: refHash('finalsPr2Live', 'fixer-attempt-1.diff'),
      ...PR2_LIVE_HONESTY,
    },
    {
      id: 'ev-live-verify', title: 'Verifier 独立验证 pr2-verify-1（VERIFIED PASS，首次通过）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['独立工作区', '全新 clone ~/pr2-verify-work @ ' + SHA.slice(0, 10) + '，git apply --check + apply 干净通过'],
        ['sha256 独立复现', '与 Fixer 一致（674356fc…16081）'],
        ['修复前基线', '3 组越权探针 200 并读到基目录外真实内容（TOP-SECRET-OUTSIDE-BASE / DEEP-SECRET / /etc/hostname）'],
        ['修复后', '越权全 400 Invalid file path · 合法文件 200 · 缺失 404'],
        ['回归语义', '冻结复现测试修复后 2 failed = 断言反转（预期，非回归）；未修改任何测试'],
        ['返工', '未发生 —— 自然首次 PASS（如实记录）'],
      ],
      blocks: [
        { title: 'finalsPr2Live/verifier-result.md', lang: 'text', text: verifierResult ?? '未提供' },
        { title: 'finalsPr2Live/verifier-workspace/verification.md', lang: 'markdown', text: verification ?? '未提供' },
      ],
      source_ref: 'finalsPr2Live/verifier-result.md',
      hash: refHash('finalsPr2Live', 'verifier-result.md'),
      ...PR2_LIVE_HONESTY,
    },
    {
      id: 'ev-live-audit', title: '运行审计 AUDIT.md（时钟漂移/异常/用量/合规）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['用量', '案例窗口 243 次调用 · 输入 9.16M（96% 缓存命中）· 估算 ¥3.3–6.5（预算 ¥10 内）'],
        ['合规', 'PR #2 全程 OPEN · head SHA 运行前后一致 · 零 GitHub 写入'],
        ['异常披露', '平台 Manager heartbeat 已停 · Leader 跳门已作废 · Fixer 自启已叫停 · 容器时钟跳变已记录换算'],
      ],
      blocks: [{ title: 'finalsPr2Live/AUDIT.md', lang: 'markdown', text: audit ?? '未提供' }],
      source_ref: 'finalsPr2Live/AUDIT.md',
      hash: refHash('finalsPr2Live', 'AUDIT.md'),
      ...PR2_LIVE_HONESTY,
    },
    {
      id: 'ev-live-rooms', title: 'Matrix 房间导出（团队房 233 事件 / Leader DM 112 事件）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['团队房', '!RErK7WVs9iUeaszwho:elemiso-matrix:6167 —— 派发/风险依据/补丁交付/验证结果全可见'],
        ['Leader DM', '!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167 —— kickoff、人工门、最终报告'],
        ['人工入口', 'Element Web http://127.0.0.1:18088（房间历史保留在 Matrix 服务器）'],
      ],
      blocks: [{ title: 'finalsPr2Live/team-room-messages.json（事件数）', lang: 'json', text: JSON.stringify({ room: teamRoom?.room, events: teamRoom?.count, note: '全文见证据目录' }) }],
      source_ref: 'finalsPr2Live/team-room-messages.json',
      hash: refHash('finalsPr2Live', 'team-room-messages.json'),
      ...PR2_LIVE_HONESTY,
    },
  ];

  const steps = [
    {
      id: 'pr-init', title: '运行发起（kickoff）',
      points: [
        { k: 'run_id', v: RUN, mono: true },
        { k: '委派链', v: 'admin → CoPaw Leader DM → taskflow(delegate_task) → 团队房间 @mention（全 Matrix 事件留痕）' },
        { k: 'head SHA', v: SHA, mono: true },
        { k: '角色', v: 'Leader=编排权威（projectflow/taskflow）；Reviewer/Fixer/Verifier=执行者（ack_task→作业→submit_task）' },
      ],
      evidence: ['ev-live-meta'],
      probe: null,
      detail: { title: '运行环境', quote: 'elemiso 隔离栈：全部镜像与 P14 证据一致（223ddc2 系），CoPaw Leader 编排；平台 Manager 停用（heartbeat 失控已处置）。', outbox: [], events: [] },
    },
    {
      id: 'review', title: '风险审查（真实执行）',
      points: [
        { k: 'Reviewer 结论', v: 'FINDING_CONFIRMED · SEVERITY: HIGH · HUMAN_VERIFICATION_REQUIRED: YES' },
        { k: '风险定性', v: 'CWE-22 路径穿越/任意文件读取 —— name 直接 join 进 DEMO_FILES_DIR，../ 可逃逸' },
        { k: '真实复现', v: '越基目录读文件 HTTP 200；Reviewer PoC 读到 /etc/hostname 真实内容' },
      ],
      evidence: ['ev-live-review'],
      probe: null,
      attack: { request: attack, legit: 'GET /demo/download?name=welcome.txt' },
      detail: { title: '执行细节', quote: 'Reviewer 在自己容器内 clone+checkout head SHA（git rev-parse 校验）后独立审查并运行真实复现测试。', outbox: [], events: [] },
    },
    {
      id: 'gate', title: '人工安全门（真实批准）',
      points: [
        { k: '门触发', v: 'Reviewer 结论 HIGH + HUMAN_VERIFICATION_REQUIRED: YES —— 系统停等，禁止派发修复' },
        { k: '本案例的真实经过', v: 'Leader 曾违规委派 fix（被操作员作废回滚）→ 操作员向用户展示真实 findings → 用户批准（批准指令 DM $5Bj24K0DHVkjc2BD2_8A9OR7Mh-U4d-PS_fw4WJ9sOM）' },
        { k: '操作员决策', v: '查看具体 findings 后批准修复+验证 —— 本次真实批准，位置在修复前，不复用历史批准' },
        { k: '门记录', v: 'human-gate-approval.md 落盘项目存储（范围+禁令+门前提 anomalies）' },
      ],
      evidence: ['ev-live-gate'],
      probe: null,
      detail: { title: '门与边界', quote: '门未批期间 Leader 曾违规委派（已作废回滚）、Fixer 曾自启（已叫停）——均留痕于 AUDIT §3，体现"门=权限拓扑"需操作员强制执行。', outbox: [], events: [] },
    },
    {
      id: 'fix-diff', title: '修复 diff（Fixer 最小修复）',
      points: [
        { k: '交付物', v: 'attempt-1.diff —— 单文件 +13/−7，测试零改动，零 GitHub 写入' },
        { k: '修复策略', v: 'realpath 归一化 + commonpath 包含性校验；越界 400 / 缺失 404 / 合法 200' },
        { k: 'patch sha256', v: '674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081', mono: true },
        { k: '门后派发链(审计修正)', v: 'Leader 重派因 Matrix 事务号幂等未产生新委派事件;Fixer 由操作员按 SPEC B 直接指令开工(偏离协议,如实披露)' },
      ],
      evidence: ['ev-live-fix'],
      probe: null,
      detail: { title: 'fixer 执行细节', quote: 'fixer 在自己容器的新目录 clone+checkout 同一 head SHA，修改单文件后 py_compile 自检、生成 diff 并提交 TaskResult。', outbox: [], events: [] },
    },
    {
      id: 'verify', title: '独立验证（VERIFIED PASS）',
      points: [
        { k: '独立工作区', v: 'Verifier 全新 clone + apply patch（sha256 与 Fixer 一致）' },
        { k: '修复前', v: '越权 3 组 200 并泄露基外内容；合法 200' },
        { k: '修复后', v: '越权全 400 · 合法 200 保留 · 缺失 404', mono: false },
        { k: '回归', v: '冻结复现测试 2 failed = 断言反转（预期），未修改测试' },
        { k: '返工', v: '未发生 —— 自然首次 PASS，如实记录' },
      ],
      evidence: ['ev-live-verify'],
      probe: {
        headline: '探针对照 · 越权读取关闭，合法访问保留（Verifier 独立实测）',
        rows: [
          { label: '越权读取（../outside/outside-secret.txt）', request: attack, before: 'HTTP 200 · 泄露 TOP-SECRET-OUTSIDE-BASE', after: 'HTTP 400 · Invalid file path', kind: 'fixed' },
          { label: '越权读取（多级 ../ 至 /etc/hostname）', request: 'name=../../../../../../etc/hostname', before: 'HTTP 200 · 泄露主机名', after: 'HTTP 400 · Invalid file path', kind: 'fixed' },
          { label: '合法基内文件(ok.txt,Verifier 实测)', request: 'GET /demo/download?name=ok.txt', before: 'HTTP 200 · LEGIT-INSIDE-BASE', after: 'HTTP 200 · 仍可用', kind: 'stable' },
        ],
      },
      detail: { title: '验证事件链', quote: 'Verifier 不信任 Fixer 自述：只采信自己的 clone、自己的 apply、自己的探针退出码与 HTTP 状态码。', outbox: [], events: [] },
    },
    {
      id: 'approval', title: 'Leader 验收 · 最终处置',
      points: [
        { k: 'Leader 验收', v: '三任务 TaskResult 全部 effective，plan 全部 [x]，项目 completed' },
        { k: '最终报告', v: 'DM $91jWHmFauP7vmlr8lYKG78wh2K2KC92eRDRr-CQwmYo —— FIX_VERIFIED' },
        { k: 'PR 状态', v: 'PR #2 保持 OPEN（运行前后 head SHA 一致，零 GitHub 写入）' },
        { k: '写入 GitHub', v: '未执行（NOT_EXECUTED）' },
      ],
      evidence: ['ev-live-audit', 'ev-live-rooms'],
      probe: null,
      decision: {
        verified: { verdict: 'PASS', text: 'VERDICT: VERIFIED —— 越权 200→400，合法 200 保留（Verifier 独立实测）' },
        human_approval: { needed: true, text: '需要且已执行 —— 本次真实操作员批准，位置在修复前', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        merge_allowed: { allowed: false, text: 'PR #2 保持 OPEN —— merge/push/close/reopen 禁止且未发生' },
        github_write: { done: false, text: '未写入 —— 零 GitHub 写入', level: 'NOT_EXECUTED' },
        execution_nature: { text: 'REAL_EXECUTED-AgentTeams（本次运行）：真实 Matrix/LLM/容器执行；本页面为该运行的只读证据回放', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        pr_open: true,
      },
      detail: { title: '终态', quote: '运行结束已停止全部计费调用者（CR state=Stopped）；房间历史/卷/CR 全部保留，Element 可复核。', outbox: [], events: [] },
    },
  ];

  return {
    case_id: 'fastapi-pr2-live-20260916',
    portfolio_role: '案例 A · R1 首跑（审计修正对照）',
    relation_note: '与主案例 fastapi-pr2-r2-live-20260916 为同一 PR 同日两次运行：本卡是 R1 首跑（其流程偏差经第三方审计并修正，修正版见 R2）。',
    shape: 'guided',
    name: 'FastAPI PR #2 · 本次真实运行（2026-09-16）',
    short_name: '主案例 · PR #2 真实运行',
    one_liner: '本次真实执行的 AgentTeams 闭环：CoPaw Leader 编排 Reviewer→人工门→Fixer→Verifier，真实 deepseek-chat 调用，首次即 VERIFIED PASS，PR 保持 OPEN。',
    repo: 'nghqqa/fastapi-boilerplate-demo',
    pr: 'PR #2 · demo/high-risk-human-gate',
    pending_redactions: [
      ['demo/high-risk-human-gate', 'demo/···'],
      ['copaw-high-risk-human-gate', 'copaw-high-risk-···'],
    ],
    pr_url: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/2',
    run_id: RUN,
    sha: SHA,
    sha_kind: 'PR #2 head commit（运行前后 GitHub API 双向核验一致）',
    evidence_level: ['REAL_EXECUTED_AGENTTEAMS_LIVE'],
    replay_note: 'REAL_EXECUTED（本次运行 2026-09-16）—— 本页面回放该次真实运行的证据；演示页面自身不连接实时系统',
    purpose: '在历史回放之外，提供一次全新的端到端真实执行证据：含真实人工门、真实补丁、独立验证与异常审计',
    risk_tags: ['CWE-22 路径穿越', '任意文件读取', 'HIGH'],
    status: { verdict: 'PASS', label: '本次真实执行 · VERIFIED PASS · PR 保持 OPEN' },
    facts: {
      real_github_pr: { value: '真实 GitHub PR（运行后核验：仍 OPEN）', ok: true, note: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/2' },
      real_agentteams_run: { value: '真实 AgentTeams 运行（2026-09-16 本次）', ok: true, note: 'CoPaw Leader 编排 · deepseek-chat · 全事件 id 留痕' },
      github_write: { value: '未写入 —— PR 保持 OPEN', ok: false, note: 'merge/push/close/reopen 全程禁止' },
    },
    banner: null,
    stage_timeline: null,
    chain: [`案例 ${RUN}`, 'PR #2', `commit ${short(SHA, 10)}`, 'demo_high_risk.py', 'attempt-1.diff', 'probe 200→400', 'VERDICT: VERIFIED'],
    generated_at: '运行 2026-09-15T17:19Z–2026-09-16T00:5xZ（UTC；墙钟 ≈7.6h，含操作员离席等待，见 AUDIT §2）',
    source_dir: 'evidence/FINALS-ELEM-PR2-LIVE-20260916（本次新证据，SHA256SUMS 锁定）',
    honesty: PR2_LIVE_HONESTY,
    steps,
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// --------------------------------------------------- case E: PR #3 human rejection (SK5-TRACED + historical 2026-08-30)

function caseEPr3Reject() {
  if (!exists('rejectDemo', 'README.md')) return null;
  const V = 'finalsPr3Sk5Traced';
  const liveAvailable = exists(V, 'kickoff-as-sent.txt');
  const rejection = exists('rejectDemo', 'human-rejection-record.json') ? readJson('rejectDemo', 'human-rejection-record.json') : null;
  const reviewResult = exists('rejectDemo', 'reviewer-high-risk-result.md') ? readText('rejectDemo', 'reviewer-high-risk-result.md') : null;
  const taskState = exists('rejectDemo', 'post-rejection-task-state.json') ? readJson('rejectDemo', 'post-rejection-task-state.json') : null;
  const planAfter = exists('rejectDemo', 'post-rejection-plan.md') ? readText('rejectDemo', 'post-rejection-plan.md') : null;
  const gateRequest = exists('rejectDemo', 'human-gate-request.json') ? readJson('rejectDemo', 'human-gate-request.json') : null;

  // live run (SK5-TRACED) artifacts — FINALS-ELEM-PR3-SK5-TRACED
  const lv = (f) => (liveAvailable && exists(V, f) ? readText(V, f) : null);
  const liveReview = lv('tasks/pr3sk5-review-1/result.md');
  const liveFindings = lv('tasks/pr3sk5-review-1/workspace/findings.md');
  const liveRejection = lv('project/human-gate-rejection.md');
  const livePlan = lv('project/plan.md');
  const liveGateSent = lv('gate-rejection-sent.json');
  const liveUsage = lv('usage-summary.json');
  const liveKickoff = lv('kickoff-as-sent.txt');
  // SK5 pack has no dedicated window export — recompute zero-dispatch from the
  // full room export over the PR3 window (17:00–17:03Z).
  const fullRoom = liveAvailable && exists(V, 'team-room-messages.json') ? readJson(V, 'team-room-messages.json') : null;
  const evAll = (x) => (Array.isArray(x) ? x : (x?.events ?? []));
  const wLo = Date.parse('2026-09-18T17:00:00Z');
  const wHi = Date.parse('2026-09-18T17:03:00Z');
  const winEvents = evAll(fullRoom).filter((e) => e?.origin_server_ts >= wLo && e?.origin_server_ts <= wHi);
  const winMsgs = winEvents.filter((e) => e?.type === 'm.room.message');
  const mentions = (who) => winMsgs.filter((e) => {
    const c = e.content || {};
    const ids = (c['m.mentions'] && c['m.mentions'].user_ids) || [];
    return ids.some((u) => String(u).includes(who)) || String(c.body || '').includes(`@${who}:`);
  }).length;
  const zeroDispatch = { window_events: winEvents.length, window_messages: winMsgs.length, fixer_mentions: mentions('fixer'), verifier_mentions: mentions('verifier') };
  const roleContracts = ['ROLE-LEADER.md','ROLE-REVIEWER.md','ROLE-FIXER.md','ROLE-VERIFIER.md'].map((rf) => ({ name: rf, text: readRepoFile('tools/agentteams/roles/' + rf) }));

  const HONESTY = {
    real: liveAvailable
      ? ['真实执行（SK5-TRACED）：Reviewer 独立审查（含确定性 skill_* 与 rag_retrieve 引用实录）、操作员拒绝（按运行前书面授权自动投递）、Leader 落实绑定效应；全部 event_id 留痕；fix/verify 从未派发（零派发三重核验）']
      : ['历史真实运行（2026-08-30）：真实 Reviewer 审查、真实操作员拒绝、真实 Leader 停等；fix-1/verify-1 从未派发'],
    controlled: ['本页面为证据的只读回放与讲解文案；拒绝为终态，演示层不可翻转'],
    not_executed: liveAvailable
      ? ['Fixer / Verifier 从未派发（两轮均 0 消费留痕）', '零 GitHub 写入；PR #3 保持 OPEN', 'direct-probe 本轮采集修复（stdin 方式，4/4 非空）——此前四轮未采集的已知操作缺陷闭环']
      : ['Fixer / Verifier 从未派发（两轮均 0 消费留痕）', '零 GitHub 写入；PR #3 保持 OPEN'],
  };

  const items = liveAvailable ? [
    {
      id: 'ev-rej-live-review', title: 'Reviewer 结论 pr3sk5-review-1（skill advisory 与自主定级并存）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['结论', 'FINDING_CONFIRMED · SEVERITY: HIGH（Reviewer 自主定级）· HUMAN_VERIFICATION_REQUIRED: YES'],
        ['定性', 'CWE-78 未认证 RCE：demo_ping f-string 拼接 host → subprocess.run(shell=True) → 输出回显；路由无鉴权'],
        ['真实复现', 'Reviewer 容器内 ; id → uid=0(root)；; | 换行 反引号 $(...) 均以 root 执行；; cat /etc/hostname 读出宿主文件；ping 缺失为环境事实而非缓解（findings.md 如实分列）'],
        ['事件', '委派 17:00:35Z $PuOp7Pzxr4gP-kqjx2MKJdaJZZ35_54ivA_EgWUUKRM · 提交 17:01:12Z（kickoff 后 44 秒）· head ad267a6e（与历史轮 ls-remote 核验一致）'],
        ['skill 实录', 'skill_risk_classify → 建议性 L1（元数据级、低估）——自主代码审查 + live PoC 确立 HIGH，分歧如实并存并被 Leader 在门报告中原话引用'],
        ['RAG 引用', '复现完成后检索组织标准（cwe-78-command-injection / command-execution 规范，references only）'],
      ],
      blocks: [
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/tasks/pr3sk5-review-1/result.md', lang: 'text', text: liveReview ?? '未提供' },
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/tasks/pr3sk5-review-1/workspace/findings.md', lang: 'text', text: liveFindings ?? '未提供' },
      ],
      source_ref: `${V}/tasks/pr3sk5-review-1/result.md`,
      hash: refHash(V, 'tasks/pr3sk5-review-1/result.md'),
      ...HONESTY,
    },
    {
      id: 'ev-rej-live-gate', title: '人工安全门 → 操作员拒绝', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['Leader 行为', '审查验收后停门（团队房向 Team Admin 报告并请求门决策；与 PR #2 批准分支同会话对照）'],
        ['操作员决策', 'HUMAN_SECURITY_REJECTED —— 不授权修复（按运行前书面授权自动投递，17:02:34Z）'],
        ['绑定效应', 'pr3sk5-fix-1 标记 [-] REJECTED（never delegated）· pr3sk5-verify-1 标记 [!] LOCKED · 项目 status=blocked（PROJECT_BLOCKED_HUMAN_REJECTED）'],
        ['事件', '拒绝记录 gate-rejection-sent.json（DM/团队房双事件号）· 团队房 17:02:34Z [HUMAN SECURITY GATE REJECTED] 实时留痕'],
        ['门记录', 'human-gate-rejection.md 落盘项目存储（四条绑定效应 + 零 GitHub 写入边界）'],
      ],
      blocks: [
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/project/human-gate-rejection.md', lang: 'text', text: liveRejection ?? '未提供' },
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/project/plan.md（拒绝后的 plan 快照）', lang: 'text', text: livePlan ?? '未提供' },
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/gate-rejection-sent.json', lang: 'json', text: liveGateSent ?? '未提供' },
      ],
      source_ref: `${V}/project/human-gate-rejection.md`,
      hash: refHash(V, 'project/human-gate-rejection.md'),
      ...HONESTY,
    },
    {
      id: 'ev-rej-live-kickoff', title: 'kickoff 实发指令（编排协议）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['实发时间', '17:00:28Z · run-elem-pr3sk5-20260919-01 · project elemiso-pr3sk5-reject'],
        ['协议要点', '严格按 DAG 顺序委派；HIGH/CRITICAL 或 HUMAN_VERIFICATION_REQUIRED → 停门等待人工决定；APPROVE 才派 pr3sk5-fix-1，REJECT 则 fix [-]/verify [!]/blocked——分支规则写死在指令里，决策结果不在这条消息中'],
        ['锁定边界', 'SK5 包 SHA256SUMS 锁定 42 项；kickoff-as-sent.txt 随包锁定；轮次总说明见 FINALS-ELEM-SK5-MASTER-README.md'],
      ],
      blocks: [
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/kickoff-as-sent.txt', lang: 'text', text: liveKickoff ?? '未提供' },
      ],
      source_ref: `${V}/kickoff-as-sent.txt`,
      hash: refHash(V, 'kickoff-as-sent.txt'),
      ...HONESTY,
    },
    {
      id: 'ev-rej-live-run', title: '运行档案（用量、追踪与合规）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['墙钟/用量', 'kickoff 17:00:28Z → 门拒绝 17:02:34Z = 2 分 06 秒 · pr3 窗口 33 次调用 · 输入 3,882,302（缓存 3,862,400 ≈99%）· 输出 9,022'],
        ['追踪（会话累计）', 'SK5 四包同会话：span-summary 824 span（leader 280 / reviewer 240 / fixer 118 / verifier 129 / reviewer-b 57）；本窗口团队房 17 事件 · Leader DM 6 事件'],
        ['零派发核验（实时复算）', `窗口 ${zeroDispatch.window_messages} 条消息 · @fixer mention = ${zeroDispatch.fixer_mentions} · @verifier mention = ${zeroDispatch.verifier_mentions} · plan [-]/[!] · 无 pr3sk5-fix-1/verify-1 任务目录`],
        ['合规', 'PR #3 全程 OPEN · github-branches-after-sk5.txt：两分支 head 运行后与记录一致 · 零 GitHub 写入'],
        ['采集披露', 'direct-probe 本轮采集修复（stdin 方式，4/4 非空 375–377 字节）——此前四轮未采集的已知操作缺陷闭环'],
      ],
      blocks: [
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/usage-summary.json（分窗口用量）', lang: 'json', text: liveUsage ?? '未提供' },
        { title: 'FINALS-ELEM-PR3-SK5-TRACED/span-summary.json（SK5 会话累计）', lang: 'json', text: lv('span-summary.json') ?? '未提供' },
        { title: '零派发复算（team-room-messages.json 按 17:00–17:03Z 窗口）', lang: 'json', text: JSON.stringify(zeroDispatch, null, 2) },
      ],
      source_ref: `${V}/gate-rejection-sent.json`,
      hash: refHash(V, 'gate-rejection-sent.json'),
      ...HONESTY,
    },
    {
      id: "ev-rej-live-roles", title: "角色契约（v1.0 冻结，跨案例零改动）", level: 'POST_RUN_DESIGN',
      fields: [
        ['契约版本', 'v1.0（冻结，跨案例零改动；回溯固化自 P14 角色矩阵与两轮真实运行的公共协议）'],
        ['要点', '角色行为与案例参数分离：Reviewer/Fixer/Verifier/Leader 契约在两个 PR 的运行中逐字相同，案例差异只在 CASE-MANIFEST（SHA/目标文件/task id/验收行为）'],
      ],
      blocks: roleContracts.map((rc) => ({ title: 'tools/agentteams/roles/' + rc.name, lang: 'markdown', text: rc.text ?? '未提供' })),
      source_ref: 'tools/agentteams/roles/（仓库文件，非案例工件）',
      ...HONESTY,
    },
    {
      id: 'ev-rej-review', title: '【历史对照 2026-08-30】Reviewer 结论（critical）', level: 'HISTORICAL_REPLAY',
      fields: [
        ['结论', 'HIGH_RISK_FOUND · SEVERITY: critical · HUMAN_VERIFICATION_REQUIRED: true'],
        ['定性', 'OS command injection / RCE：host 参数 shell 注入（与本次独立审查同一缺陷）'],
        ['PR', '#3 · head ad267a6e51209551a0733657321bb364d04befd0（全程 OPEN 未合并）'],
      ],
      blocks: [{ title: 'rejectDemo/reviewer-high-risk-result.md', lang: 'text', text: reviewResult ?? '未提供' }],
      source_ref: 'rejectDemo/reviewer-high-risk-result.md',
      hash: refHash('rejectDemo', 'reviewer-high-risk-result.md'),
      ...HONESTY,
    },
    {
      id: 'ev-rej-rejection', title: '【历史对照】人工拒绝记录（终态）', level: 'REAL_EXECUTED_AGENTTEAMS', postgate: true,
      fields: [
        ['决策', rejection?.decision ?? 'HUMAN_SECURITY_REJECTED'],
        ['决策人 / 时间', `${rejection?.decided_by ?? 'operator'} · ${rejection?.decided_at_utc ?? '未提供'}`],
        ['效果', 'fix-1 = rejected · verify-1 = locked · project = blocked(paused)；Fixer/Verifier 从未启动（0 消费留痕）'],
      ],
      blocks: [
        { title: 'rejectDemo/human-rejection-record.json', lang: 'json', text: rejection ? JSON.stringify(rejection, null, 2) : '未提供' },
        { title: 'rejectDemo/human-gate-request.json', lang: 'json', text: gateRequest ? JSON.stringify(gateRequest, null, 2) : '未提供' },
      ],
      source_ref: 'rejectDemo/human-rejection-record.json',
      hash: refHash('rejectDemo', 'human-rejection-record.json'),
      ...HONESTY,
    },
    {
      id: 'ev-rej-state', title: '【历史对照】拒绝后的系统状态（不再派发）', level: 'REAL_EXECUTED_AGENTTEAMS', postgate: true,
      fields: [
        ['plan', 'fix-1 标记 rejected · verify-1 保持 locked（见 post-rejection-plan.md）'],
        ['项目状态', 'blocked(paused) —— 系统没有继续执行'],
        ['审计', 'pre/post rejection 的 fixer/verifier activity 对照：零消费'],
      ],
      blocks: [
        { title: 'rejectDemo/post-rejection-plan.md', lang: 'text', text: planAfter ?? '未提供' },
        { title: 'rejectDemo/post-rejection-task-state.json', lang: 'json', text: taskState ? JSON.stringify(taskState, null, 2) : '未提供' },
      ],
      source_ref: 'rejectDemo/post-rejection-task-state.json',
      hash: refHash('rejectDemo', 'post-rejection-task-state.json'),
      ...HONESTY,
    },
  ] : [
    {
      id: 'ev-rej-review', title: 'Reviewer 结论（critical · CWE-78）', level: 'REAL_EXECUTED_AGENTTEAMS',
      fields: [
        ['结论', 'HIGH_RISK_FOUND · SEVERITY: critical · HUMAN_VERIFICATION_REQUIRED: true'],
        ['定性', 'OS command injection / RCE：user-controlled host 以 f-string 拼进 ping 命令并 subprocess.run(shell=True)'],
        ['位置', 'backend/src/interfaces/api/v1/demo_cmd_exec.py demo_ping'],
        ['PR', '#3 · head ad267a6e51209551a0733657321bb364d04befd0（全程 OPEN 未合并）'],
      ],
      blocks: [{ title: 'rejectDemo/reviewer-high-risk-result.md', lang: 'text', text: reviewResult ?? '未提供' }],
      source_ref: 'rejectDemo/reviewer-high-risk-result.md',
      hash: refHash('rejectDemo', 'reviewer-high-risk-result.md'),
      ...HONESTY,
    },
    {
      id: 'ev-rej-rejection', title: '人工拒绝记录（终态）', level: 'REAL_EXECUTED_AGENTTEAMS', postgate: true,
      fields: [
        ['决策', rejection?.decision ?? 'HUMAN_SECURITY_REJECTED'],
        ['决策人 / 时间', `${rejection?.decided_by ?? 'operator'} · ${rejection?.decided_at_utc ?? '未提供'}`],
        ['送达', 'Leader DM event + store 落盘 + read-back 确认'],
        ['效果', 'fix-1 = rejected · verify-1 = locked · project = blocked(paused)；Fixer/Verifier 从未启动'],
      ],
      blocks: [
        { title: 'rejectDemo/human-rejection-record.json', lang: 'json', text: rejection ? JSON.stringify(rejection, null, 2) : '未提供' },
        { title: 'rejectDemo/human-gate-request.json', lang: 'json', text: gateRequest ? JSON.stringify(gateRequest, null, 2) : '未提供' },
      ],
      source_ref: 'rejectDemo/human-rejection-record.json',
      hash: refHash('rejectDemo', 'human-rejection-record.json'),
      ...HONESTY,
    },
    {
      id: 'ev-rej-state', title: '拒绝后的系统状态（不再派发）', level: 'REAL_EXECUTED_AGENTTEAMS', postgate: true,
      fields: [
        ['plan', 'fix-1 标记 rejected · verify-1 保持 locked（见 post-rejection-plan.md）'],
        ['项目状态', 'blocked(paused) —— 系统没有继续执行'],
        ['审计', 'pre/post rejection 的 fixer/verifier activity 对照：零消费'],
      ],
      blocks: [
        { title: 'rejectDemo/post-rejection-plan.md', lang: 'text', text: planAfter ?? '未提供' },
        { title: 'rejectDemo/post-rejection-task-state.json', lang: 'json', text: taskState ? JSON.stringify(taskState, null, 2) : '未提供' },
      ],
      source_ref: 'rejectDemo/post-rejection-task-state.json',
      hash: refHash('rejectDemo', 'post-rejection-task-state.json'),
      ...HONESTY,
    },
  ];

  const steps = liveAvailable ? [
    {
      id: 'pr-init', title: '⓪ PR 接入 · 编排下发', role: 'Leader', time: '17:00:28Z kickoff',
      headline: 'PR #3 接入即建 run——同一套编排管线，任务书 7 秒下到 Reviewer',
      stat: 'SPEC 已下发',
      hand: 'kickoff DM → Leader · manifest 声明确定性 Skill + 知识库（advisory-only）',
      points: [
        { k: 'PR 内容', v: 'demo_cmd_exec.py 新增 demo_ping 端点（2 files · +82/-0）· head ad267a6e' },
        { k: 'run_id', v: 'run-elem-pr3sk5-20260919-01', mono: true },
        { k: '编排', v: 'kickoff 17:00:28Z → 委派 pr3sk5-review-1 @reviewer 17:00:35Z' },
        { k: '契约', v: '角色契约 v1.0 冻结 · 跨案例零改动——SPEC 不下结论，定级权留给下游' },
      ],
      evidence: ['ev-rej-live-kickoff', 'ev-rej-live-roles', 'ev-rej-live-run'],
      probe: null,
      detail: { title: '环境披露', quote: '与 PR #2 同一编排管线、同一 Skills + 知识库能力声明（SK5 同会话四线并行）；SPEC 不含任何技能提示的结论，定级权留给下游。', outbox: [], events: [] },
    },
    {
      id: 'review', title: '① 风险审查', role: 'Reviewer', time: 'kickoff 后 44 秒提交',
      headline: '一分钟内独立确认未认证 RCE——确定性 Skill 建议与自主定级并存',
      stat: '确认 HIGH · CWE-78',
      hand: '委派 pr3sk5-review-1 → @reviewer · 独立审查任务',
      points: [
        { k: 'Reviewer 结论', v: 'FINDING_CONFIRMED · SEVERITY: HIGH · HUMAN_VERIFICATION_REQUIRED: YES' },
        { k: '风险定性', v: 'CWE-78 未认证 RCE —— ; | 换行 反引号 $(...) 全部以 root 执行（; id → uid=0(root) 实证；ping 缺失为环境事实而非缓解）' },
        { k: 'skill 实录', v: 'skill_risk_classify 建议性 L1（元数据级、低估）——自主审查 + live PoC 确立 HIGH，分歧如实并存' },
        { k: '时效', v: 'kickoff 17:00:28Z → 委派 17:00:35Z → 提交 17:01:12Z（kickoff 后 44 秒）' },
      ],
      evidence: ['ev-rej-live-review'],
      probe: null,
      detail: { title: '独立审查与检索', quote: 'Reviewer 在自己容器 clone+checkout head ad267a6e 后独立审查；复现完成后检索 cwe-78 组织标准（references only）；历史轮（2026-08-30）同缺陷被定级 critical——两轮独立定级如实分列。', outbox: [], events: [] },
    },
    {
      id: 'rag', title: '② RAG 与确定性 Skill', role: 'RAG · Skills', time: '审查期内',
      headline: '检索引用组织规范、工具给建议——citation-only，引用与建议都不替代验证',
      stat: '规范引用落盘',
      hand: 'RAG MCP · rag_retrieve 引用组织规范 · references only',
      points: [
        { k: '检索', v: 'rag_retrieve 引用 cwe-78-command-injection / command-execution 组织规范（findings 标注 references only）' },
        { k: 'skill 建议', v: 'skill_risk_classify 建议性 L1——Leader 在门报告中原话引用该分歧；建议性输出不覆盖自主 HIGH' },
        { k: '性质', v: '引用与建议提供依据；风险定级与 PoC 复现由 Reviewer 独立完成' },
      ],
      evidence: ['ev-rej-live-review'],
      probe: null,
      detail: { title: '检索方式', quote: '与平台 /api/rag/search 同形：citation-only；RAG 引用逐条留痕于 findings.md。', outbox: [], events: [] },
    },
    {
      id: 'gate', title: '③ 人工门 · 操作员真实拒绝（终态）', role: '人工决策', time: '17:02:34Z 拒绝', tone: 'reject',
      hand: 'TaskResult pr3sk5-review-1 → @leader · HIGH 触发停门 · fix/verify 计划锁定待决',
      headline: '拒绝即终态——修复与验证从未派发',
      points: [
        { k: 'Leader 行为', v: '审查验收后停门等决策（门报告原话引用 skill_risk_classify 与自主 HIGH 的分歧；与 PR #2 批准分支同会话对照）' },
        { k: '操作员决策', v: 'HUMAN_SECURITY_REJECTED —— 拒绝修复授权（17:02:34Z，按运行前书面授权自动投递）' },
        { k: '绑定效应', v: 'pr3sk5-fix-1 标记 [-] rejected（never delegated）· pr3sk5-verify-1 标记 [!] locked · 项目 blocked', mono: true },
        { k: '终态不可翻转', v: '拒绝即终态：系统停等，全部审查证据保留，零 GitHub 写入' },
      ],
      evidence: ['ev-rej-live-gate', 'ev-rej-live-run'],
      probe: null,
      exhibit: {
        type: 'ticket',
        title: '人工安全门拒绝记录 · project/human-gate-rejection.md（先于任何派发落盘）',
        stamp: 'REJECTED', stampTone: 'error',
        rows: [
          ['决策时间', '17:02:34Z（UTC）—— 交互式门提示，操作员在场响应'],
          ['决策人', 'repository / workspace owner（human operator）'],
          ['审查对象', 'pr3sk5-review-1 · head SHA ad267a6e…efd0（PR #3）'],
          ['后果绑定', 'fix rejected（从未派发）· verify locked · 项目 blocked'],
        ],
        foot: 'HUMAN_SECURITY_REJECTED —— no remediation authorized · 终态不可翻转 · 零 GitHub 写入',
      },
      decision: {
        verified: { verdict: 'NOT_EXECUTED', text: '验证未执行 —— 人工拒绝后 Fixer/Verifier 从未派发（两轮一致；窗口 mention 实时复算 = 0）' },
        human_approval: { needed: true, text: '已执行，结果为拒绝（17:02:34Z，操作员运行前授权的拒绝分支，自动投递）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        merge_allowed: { allowed: false, text: 'PR #3 保持 OPEN —— 未合并；拒绝即停止' },
        github_write: { done: false, text: '未写入 GitHub', level: 'NOT_EXECUTED' },
        execution_nature: { text: 'REAL_EXECUTED-AgentTeams + HISTORICAL_REPLAY（2026-08-30 对照）：两轮真实拒绝', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        pr_open: true,
      },
      detail: { title: '终态', quote: '失败安全设计：拒绝 → 保留全部审查证据 → 不产生任何修复或推送。', outbox: [], events: [] },
    },
    {
      id: 'trace', title: '④ 追踪与归档', role: 'AgentLoop', time: '全程落盘',
      headline: '拒绝与 blocked 全程落盘——归档含零派发实时复算',
      stat: '归档 + 零派发复算',
      hand: 'run 归档 · SHA256SUMS 锁定 · 窗口复算零派发',
      points: [
        { k: '用量', v: 'LLM 33 次（pr3 窗口）· 输入 3.88M（≈99% 命中缓存）' },
        { k: '零派发复算', v: '窗口 16 条消息 · @fixer / @verifier mention = 0 · 无 pr3sk5-fix-1/verify-1 任务目录' },
        { k: '归档', v: 'FINALS-ELEM-PR3-SK5-TRACED/ —— rejection 记录 + usage + 会话 span 锁定' },
      ],
      evidence: ['ev-rej-live-run'],
      probe: null,
      detail: { title: '归档内容', quote: '拒绝不是删除现场——kickoff/委派/审查/停门/拒绝落实全链留痕；窗口 17 events / 16 messages 实时复算零派发。', outbox: [], events: [] },
    },
  ] : [
    {
      id: 'review', title: '风险审查（critical）',
      points: [
        { k: 'Reviewer 结论', v: 'HIGH_RISK_FOUND · SEVERITY: critical · HUMAN_VERIFICATION_REQUIRED: true' },
        { k: '风险定性', v: 'CWE-78 命令注入/RCE —— host 参数 shell 注入' },
        { k: '派发', v: '真实 Matrix 委派，Reviewer 独立执行（历史运行 2026-08-30）' },
      ],
      evidence: ['ev-rej-review'],
      probe: null,
      detail: { title: '与 PR #2 对照', quote: '与 PR #2 同一套角色/协议；差异仅在结论等级（critical）与人工决策（拒绝）。', outbox: [], events: [] },
    },
    {
      id: 'gate', title: '人工门 · 拒绝（终态）',
      points: [
        { k: '人工决策', v: 'HUMAN_SECURITY_REJECTED —— 操作员拒绝修复授权' },
        { k: '系统效果', v: 'fix-1 rejected · verify-1 locked · project blocked(paused)', mono: true },
        { k: '不再派发', v: 'Fixer/Verifier 零消费（pre/post activity 对照留痕）' },
        { k: '终态不可翻转', v: '拒绝后任何"批准"操作 → 409 REJECTED_CASE_TERMINAL' },
      ],
      evidence: ['ev-rej-rejection', 'ev-rej-state'],
      probe: null,
      decision: {
        verified: { verdict: 'NOT_EXECUTED', text: '验证未执行 —— 人工拒绝后 Fixer/Verifier 从未派发' },
        human_approval: { needed: true, text: '已执行且结果为拒绝 —— HUMAN_SECURITY_REJECTED（终态）', level: 'REAL_EXECUTED_AGENTTEAMS' },
        merge_allowed: { allowed: false, text: 'PR #3 保持 OPEN —— 未合并；拒绝即停止' },
        github_write: { done: false, text: '未写入 GitHub', level: 'NOT_EXECUTED' },
        execution_nature: { text: 'HISTORICAL_REPLAY · REAL_EXECUTED-AgentTeams：回放 2026-08-30 真实拒绝案例', level: 'HISTORICAL_REPLAY' },
        pr_open: true,
      },
      detail: { title: '终态', quote: '失败安全设计：拒绝 → 保留全部审查证据 → 不产生任何修复或推送。', outbox: [], events: [] },
    },
  ];

  return {
    case_id: 'fastapi-pr3-reject',
    portfolio_role: '人工拒绝分支',
    shape: 'guided',
    name: liveAvailable ? 'FastAPI PR #3 · 高风险变更 · 人工拒绝停等' : 'FastAPI PR #3 · 人工拒绝（短案例）',
    name_pending: liveAvailable ? 'FastAPI PR #3 · 高风险变更审修' : 'FastAPI PR #3 · 高危变更（短案例）',
    short_name: liveAvailable ? 'PR #3 · 拒绝路径' : '第二案例 · PR #3 人工拒绝',
    one_liner: liveAvailable
      ? '真实执行：Reviewer 在 kickoff 后 44 秒独立确认 HIGH（CWE-78 未认证 RCE，; id → uid=0(root) 实证）→ 人工门拒绝 → 修复与验证从未派发（零派发三重核验）；skill_risk_classify 建议性 L1 与自主 HIGH 的分歧被 Leader 在门报告原话引用。'
      : '历史真实运行：Reviewer 确认 critical（CWE-78/RCE）→ 人工门拒绝 → 系统停等，Fixer/Verifier 从未派发，PR 保持 OPEN。',
    repo: 'nghqqa/fastapi-boilerplate-demo',
    pr: 'PR #3 · demo/high-risk-human-reject',
    // Outcome fingerprints embedded INSIDE otherwise pre-gate artifacts
    // (kickoff text, historical-run paths). Masked in every pending-gate
    // render path — preview, drawer fields/blocks/source_ref, stage hero.
    pending_redactions: [
      ['elemiso-pr3sk5-reject', 'elemiso-pr3sk5-···'],
      ['copaw-high-risk-human-reject', 'copaw-high-risk-···'],
      ['demo/high-risk-human-reject', 'demo/···'],
      ['rejectDemo', 'p14-evidence'],
      ['HIGH-RISK-REJECT', 'HIGH-RISK-···'],
      ['人工安全门 → 操作员拒绝', '人工安全门 · 决策记录'],
      ['mark fix [-] rejected, verify [!] locked, project blocked', '…'],
      ['fix [-]/verify [!]/blocked', 'fix/verify 冻结·项目停等'],
      ['rejected', 'held'],
      ['blocked', 'held'],
    ],
    pr_url: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/3',
    run_id: liveAvailable ? 'run-elem-pr3sk5-20260919-01' : 'copaw-high-risk-reject-demo（AgentTeams project）',
    sha: liveAvailable ? 'ad267a6e51209551a0733657321bb364d04befd0' : (rejection?.source_commit?.replace('PR#3 head ', '') ?? null),
    sha_kind: 'PR #3 head commit · ls-remote 已核验',
    evidence_level: ['REAL_EXECUTED_AGENTTEAMS_LIVE'],
    replay_note: liveAvailable
      ? 'REAL_EXECUTED + 历史对照 —— 拒绝语义为终态，不可在演示层翻转'
      : 'HISTORICAL_REPLAY —— 回放 2026-08-30 历史运行证据；拒绝语义为终态，不可在演示层翻转',
    purpose: '展示人工门的拒绝分支：失败安全（fail-safe）——不批准即停止，证据保留',
    risk_tags: ['CWE-78 命令注入', 'RCE', 'HIGH', '确定性 Skill MCP'],
    status: { verdict: 'REJECTED', label: '真实拒绝 · 项目 blocked · Fixer/Verifier 未派发' },
    pitch_next: { to: '/demo/fastapi-pr1-auto?stage=1', kicker: '下一幕', label: '低风险自动路径 · 第三个 PR' },
    gate_alt: { label: 'PR #2 · 同一门的批准路径', path: '/demo/fastapi-pr2-rag-traced-20260917' },
    stats: [
      { k: '端到端墙钟', v: '2 分 06 秒', hint: 'kickoff → 门拒绝（拒绝即终态）' },
      { k: 'LLM 调用', v: '33 次', hint: 'pr3 窗口 · 输入 3.88M · ≈99% 命中缓存' },
      { k: '审查时效', v: '44 秒', hint: 'kickoff → Reviewer 提交' },
      { k: 'Fixer / Verifier', v: '零派发', hint: '房间/plan/任务目录三重核验' },
    ],
    facts: {
      real_github_pr: { value: '真实 GitHub PR（运行后核验：仍 OPEN）', ok: true, note: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/3' },
      real_agentteams_run: { value: liveAvailable ? '真实 AgentTeams 运行（本次最新一轮）' : '真实 AgentTeams 运行（2026-08-30 历史证据）', ok: true, note: '真实审查（含 skill_* 与 rag_retrieve 引用）、真实拒绝、真实停等' },
      github_write: { value: '未写入 —— PR 保持 OPEN', ok: false, note: '全程禁止 merge/push/close' },
    },
    banner: 'REJECTED —— 人工安全门拒绝为终态；系统没有继续执行（fail-safe 分支演示）',
    stage_timeline: null,
    chain: liveAvailable
      ? ['案例 run-elem-pr3sk5-20260919-01', 'PR #3', 'demo_cmd_exec.py', 'HUMAN_SECURITY_REJECTED（操作员真实决策）', 'fix rejected / verify locked', 'project blocked']
      : ['案例 fastapi-pr3-reject', 'PR #3', 'demo_cmd_exec.py', 'HUMAN_SECURITY_REJECTED', 'fix-1 rejected / verify-1 locked', 'project blocked'],
    generated_at: liveAvailable ? '运行窗口 17:00Z–17:02Z（本轮，kickoff → 门拒绝 2 分 06 秒）· 历史对照 2026-08-30' : '运行 2026-08-30 · 状态锁定（post-rejection-* 快照）',
    source_dir: liveAvailable ? 'evidence/FINALS-ELEM-PR3-SK5-TRACED（SHA256SUMS 锁定，42 清单项）+ PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913（历史对照）' : 'evidence/PHASE14-WINDOWS-COPAW-HIGH-RISK-REJECT-20260830-091913（rejectDemo）',
    honesty: HONESTY,
    steps,
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// --------------------------------------------------- case F: RAG eval→optimize→backtest loop

function caseFRagLoop() {
  if (!exists('finalsRagLoop', 'report.md')) return null;
  const r = readJson('finalsRagLoop', 'report.json');
  const reportMd = readText('finalsRagLoop', 'report.md');
  const runMeta = readJson('finalsRagLoop', 'run-meta.json');
  const timings = readJson('finalsRagLoop', 'timings.json');
  const queriesFile = readText('finalsRagLoop', 'queries.v1.json');
  const pct = (x) => (typeof x === 'number' ? `${(x * 100).toFixed(1)}%` : '—');
  const s = r.observation, d = r.dataset, b = r.baseline, t = r.tuning, bt = r.backtest, cmp = bt.comparison;
  const gridRows = t.grid.map((c) => `${c.strategy.id}  hit@1=${pct(c.hit_at_1)}  hit@3=${pct(c.hit_at_3)}  MRR=${c.mrr.toFixed(4)}  chunk-hit@1=${pct(c.chunk_hit_at_1)}${c.strategy.id === t.selected.strategy.id ? '   ← 选中' : ''}`).join('\n');
  const badRows = r.badcases.map((c) => `${c.query_id}  期望 ${c.expected_document_id} → 实际 top1 ${c.retrieved_top1_chunk_id}（名次 ${c.expected_doc_rank}）`).join('\n');

  const HONESTY = {
    real: ['真实离线实验（2026-09-14）：真实 span 审计、真实检索器执行、真实指标计算；查询集在任何测量前固定，held-out 只评估一次'],
    controlled: ['语料为 SYNTHETIC/REDACTED 演示文档（8 文档/9 chunk）；标注由团队编写'],
    not_executed: ['不外推企业语料；本实验优化检索策略，不改变数据模式声明'],
  };

  const items = [
    {
      id: 'ev-rag-observation', title: '① 观测核验 tool-spans.jsonl（真实审计记录）', level: 'REAL_OFFLINE_EXPERIMENT',
      fields: [
        ['span 总行数', `${s.total_span_rows} 行（rag.retrieve ${s.rag_retrieve_rows} / rag.answer ${s.rag_answer_rows}）`],
        ['关键结论', `retrieve 127 行只对应 ${s.rag_retrieve_distinct_query_hashes} 个不同 query_hash —— 行数是重复演示的审计记录，不是独立样本`],
        ['隐私边界', '日志只存 query_hash，无法反推查询文本'],
        ['延迟', 'retrieve p50=0ms · p95=1ms'],
      ],
      blocks: [{ title: 'FINALS-RAG-LOOP-20260914/report.md（§1 观测核验）', lang: 'markdown', text: reportMd }],
      source_ref: 'finalsRagLoop/report.md',
      hash: refHash('finalsRagLoop', 'report.md'),
      ...HONESTY,
    },
    {
      id: 'ev-rag-dataset', title: '② 标注数据集与按族切分（防泄漏）', level: 'REAL_OFFLINE_EXPERIMENT',
      fields: [
        ['查询集', `queries.v1 · ${d.n_families} 个语义族 / ${d.n_queries} 条查询（版本 ${d.version}）`],
        ['切分', `按族切分（从不按条）：调优 ${d.n_tuning_families} 族/${d.n_tuning_queries} 条 · held-out ${d.n_heldout_families} 族/${d.n_heldout_queries} 条`],
        ['泄漏检查', `held-out 与调优查询的最大词元 Jaccard max=${d.leakage.max}（阈值 0.5），近似改写对 ${d.leakage.flagged_pairs.length} 对`],
        ['指标', '文档级 hit@1 / hit@3 / MRR；多 chunk 文档另计 chunk-hit@1'],
      ],
      blocks: [{ title: 'FINALS-RAG-LOOP-20260914/queries.v1.json（前 2000 字）', lang: 'json', text: queriesFile.slice(0, 2000) + '\n…' }],
      source_ref: 'finalsRagLoop/queries.v1.json',
      hash: refHash('finalsRagLoop', 'queries.v1.json'),
      ...HONESTY,
    },
    {
      id: 'ev-rag-baseline', title: '③ 基线 v1-unigram-hash256 + badcase 归因', level: 'REAL_OFFLINE_EXPERIMENT',
      fields: [
        ['调优集 (n=30)', `hit@1=${pct(b.tuning.hit_at_1)} · hit@3=${pct(b.tuning.hit_at_3)} · MRR=${b.tuning.mrr.toFixed(4)} · chunk-hit@1=${pct(b.tuning.chunk_hit_at_1)}`],
        ['held-out (n=24)', `hit@1=${pct(b.heldout.hit_at_1)} · hit@3=${pct(b.heldout.hit_at_3)} · MRR=${b.heldout.mrr.toFixed(4)}`],
        ['badcase', `基线未命中 top-1 的 ${r.badcases.length} 条全部逐条归因（期望文档名次/分差/词元重叠）`],
      ],
      blocks: [{ title: '调优集 9 条 badcase（逐条）', lang: 'text', text: badRows || '未提供' }],
      source_ref: 'finalsRagLoop/report.json#badcases',
      hash: refHash('finalsRagLoop', 'report.json'),
      ...HONESTY,
    },
    {
      id: 'ev-rag-tuning', title: '④ 策略优化（12 候选网格，只用调优集）', level: 'REAL_OFFLINE_EXPERIMENT',
      fields: [
        ['网格', 'CJK 二元组 × IDF × 哈希维度，共 12 个候选策略'],
        ['选择规则（回测前声明）', t.selection_rule ?? 'MRR → hit@1 → hit@3 → 更小维度'],
        ['选中', `${t.selected.strategy.id}（dim=${t.selected.strategy.dim} · cjk_bigram=${t.selected.strategy.cjk_bigram} · idf=${t.selected.strategy.idf}）调优集 MRR ${b.tuning.mrr.toFixed(4)} → ${t.selected.mrr.toFixed(4)}`],
      ],
      blocks: [
        { title: '12 候选 · 调优集指标全表', lang: 'text', text: gridRows },
        { title: 'FINALS-RAG-LOOP-20260914/timings.json', lang: 'json', text: JSON.stringify(timings, null, 2) },
      ],
      source_ref: 'finalsRagLoop/report.json#tuning',
      hash: refHash('finalsRagLoop', 'report.json'),
      ...HONESTY,
    },
    {
      id: 'ev-rag-backtest', title: '⑤ held-out 回测（只评估一次）+ 晋级判定', level: 'REAL_OFFLINE_EXPERIMENT',
      fields: [
        ['基线 held-out', `hit@1=${pct(b.heldout.hit_at_1)} · hit@3=${pct(b.heldout.hit_at_3)} · MRR=${b.heldout.mrr.toFixed(4)}`],
        ['选中策略 held-out', `hit@1=${pct(bt.heldout.hit_at_1)} · hit@3=${pct(bt.heldout.hit_at_3)} · MRR=${bt.heldout.mrr.toFixed(4)}`],
        ['逐条变化', `改善 ${cmp.improved.length} 条 · 退化 ${cmp.regressed.length} 条（${cmp.regressed.map((x) => x.query_id).join('、')}）· 不变 ${cmp.unchanged_count} 条`],
        ['晋级判定', `${bt.promotion.decision} —— 规则：heldout.mrr > baseline && heldout.hit@1 >= baseline（回测前声明）`],
        ['诚实边界', '语料为 SYNTHETIC/REDACTED 演示文档；结论=该策略在此合成语料上更优，不外推企业语料'],
      ],
      blocks: [{ title: 'FINALS-RAG-LOOP-20260914/run-meta.json', lang: 'json', text: JSON.stringify(runMeta, null, 2) }],
      source_ref: 'finalsRagLoop/report.json#backtest',
      hash: refHash('finalsRagLoop', 'report.json'),
      ...HONESTY,
    },
  ];

  const steps = [
    {
      id: 'observe', title: '① 观测：span 审计定界样本', role: 'AgentLoop 追踪', time: '604 行 span',
      headline: '先定界再评估——127 行 retrieve 只对应 12 个 query_hash，演示流量≠独立样本',
      points: [
        { k: '审计记录', v: `604 行 span（rag.retrieve 127 / rag.answer 26 / database.* 451）` },
        { k: '关键发现', v: '127 行 retrieve 只对应 12 个不同 query_hash —— 演示流量≠独立样本', },
        { k: '结论', v: '评估样本必须单独标注；日志只存 query_hash（隐私边界）' },
      ],
      evidence: ['ev-rag-observation'],
      probe: null,
      detail: { title: '观测来源', quote: 'span 日志=生产检索器的真实审计记录；本步骤避免"拿演示日志当评测集"的常见错误。', outbox: [], events: [] },
    },
    {
      id: 'dataset', title: '② 数据集：18 语义族 · 54 查询 · 按族切分', role: '数据集构建', time: 'v1 版本化',
      headline: '查询集在任何基线测量前固定——按族切分防泄漏，Jaccard 0.2581 < 阈值 0.5',
      points: [
        { k: '版本化查询集', v: 'rag-loop-queries.v1（任何基线测量之前固定）' },
        { k: '切分', v: '调优 10 族/30 条 · held-out 8 族/24 条 —— 按族切分，从不按条', mono: true },
        { k: '泄漏检查', v: '最大词元 Jaccard 0.2581（阈值 0.5）· 近似改写对 0', mono: true },
      ],
      evidence: ['ev-rag-dataset'],
      probe: null,
      detail: { title: '切分规则原文', quote: d.split_rule, outbox: [], events: [] },
    },
    {
      id: 'baseline', title: '③ 基线评估 + badcase 归因', role: '基线评估', time: 'v1-unigram-hash256',
      headline: '不只看指标——9 条未命中逐条归因，把「没命中」变成可优化的方向',
      points: [
        { k: '基线策略', v: 'v1-unigram-hash256（生产策略）' },
        { k: '调优集', v: `hit@1 ${pct(b.tuning.hit_at_1)} · MRR ${b.tuning.mrr.toFixed(4)}` },
        { k: 'held-out', v: `hit@1 ${pct(b.heldout.hit_at_1)} · MRR ${b.heldout.mrr.toFixed(4)}` },
        { k: 'badcase', v: `9 条未命中逐条归因（期望名次/分差/与命中块词元重叠）` },
      ],
      evidence: ['ev-rag-baseline'],
      probe: null,
      detail: { title: '方法说明', quote: '只看指标会掩盖语义错误；badcase 逐条归因才能把"未命中原因"变成优化方向。', outbox: [], events: [] },
    },
    {
      id: 'tuning', title: '④ 优化：12 候选网格（仅调优集）', role: '策略优化', time: '12 候选',
      headline: '网格搜索只许看调优集——选择规则（MRR→hit@1→hit@3→小维度）回测前声明',
      points: [
        { k: '网格维度', v: 'CJK 二元组 × IDF × 哈希维度（256/1024/4096）' },
        { k: '选择规则', v: 'MRR → hit@1 → hit@3 → 更小维度（回测前声明）', mono: true },
        { k: '选中', v: 'cand-bigram-idf-hash4096 —— 调优集 MRR 0.7928 → 0.8594，chunk-hit@1 50% → 83.3%' },
      ],
      evidence: ['ev-rag-tuning'],
      probe: null,
      detail: { title: '防过拟合纪律', quote: '策略选择只允许看调优集；held-out 在晋级判定前从未参与任何选择。', outbox: [], events: [] },
    },
    {
      id: 'backtest', title: '⑤ held-out 回测 + 晋级判定', role: '回测判定', time: '只评一次',
      headline: 'held-out 全程未参与选择——MRR 0.861→0.958 满足回测前声明的晋级规则',
      points: [
        { k: '回测结果', v: `MRR ${b.heldout.mrr.toFixed(4)} → ${bt.heldout.mrr.toFixed(4)} · hit@1 ${pct(b.heldout.hit_at_1)} → ${pct(bt.heldout.hit_at_1)}`, mono: true },
        { k: '逐条变化', v: `改善 ${cmp.improved.length} · 退化 ${cmp.regressed.length}（F17-q1 名次 1→2）· 不变 ${cmp.unchanged_count}` },
        { k: '晋级', v: `${bt.promotion.decision}（规则回测前声明，held-out 只评估一次）` },
        { k: '诚实边界', v: '合成语料小样本：单条查询名次变化即影响 4.2% hit@1；结论不外推企业语料' },
      ],
      evidence: ['ev-rag-backtest'],
      exhibit: {
        type: 'delta',
        label: 'held-out 黄金集（n=24 · 全程未参与选择 · 只评估一次）',
        metrics: [
          { name: 'MRR', from: b.heldout.mrr.toFixed(4), to: bt.heldout.mrr.toFixed(4) },
          { name: 'hit@1', from: pct(b.heldout.hit_at_1), to: pct(bt.heldout.hit_at_1) },
          { name: 'hit@3', from: pct(b.heldout.hit_at_3), to: pct(bt.heldout.hit_at_3) },
        ],
        verdict: bt.promotion.decision,
        sub: '满足回测前声明的晋级规则（MRR → hit@1 → hit@3 → 更小维度）',
      },
      decision: {
        verified: { verdict: 'PROMOTE', text: 'held-out MRR 0.8611→0.9583 且 hit@1 0.75→0.9167 —— 满足回测前声明的晋级规则' },
        human_approval: { needed: false, text: '无需人工门 —— 离线检索策略实验；数据模式（SYNTHETIC/REDACTED）声明不变', level: 'REAL_OFFLINE_EXPERIMENT' },
        merge_allowed: { allowed: true, text: '策略已晋级为新的生产检索配置（rag.mjs 可按选中参数加载）' },
        github_write: { done: false, text: '未写入 GitHub —— 纯本地实验', level: 'NOT_EXECUTED' },
        execution_nature: { text: '真实离线实验（合成语料）：真实 span 审计 + 真实检索器 + 真实指标；非 AgentTeams 运行', level: 'REAL_OFFLINE_EXPERIMENT' },
        pr_open: false,
      },
      detail: { title: '复现', quote: 'node demo-platform/backend/experiments/rag-loop/rag_eval_loop.mjs --out <dir>（报告 JSON sha256 见目录 SHA256SUMS；timings 非确定性故排除在 SUMS 外）', outbox: [], events: [] },
    },
  ];

  return {
    case_id: 'rag-retrieval-loop',
    portfolio_role: '附录 · 观测→评估→优化→回测闭环',
    shape: 'guided',
    name: 'RAG 检索闭环 · 观测→评估→数据集→优化→回测',
    short_name: '闭环能力 · RAG 检索优化',
    one_liner: '真实离线实验：span 审计定界样本 → 18 族 54 查询按族切分 → 基线+9 条 badcase 归因 → 12 候选网格调优 → held-out 只评一次，MRR 0.861→0.958 判定 PROMOTE。',
    repo: 'mergepilot/demo-platform（rag-data 合成语料）',
    pr: '本地离线实验 · 无外部 PR（合成语料）',
    pr_url: null,
    run_id: 'rag-loop-20260914',
    sha: d.corpus_sha256 ?? null,
    sha_kind: '语料 corpus sha256',
    evidence_level: ['REAL_OFFLINE_EXPERIMENT', 'SYNTHETIC'],
    replay_note: 'REAL_OFFLINE_EXPERIMENT —— 本页面回放 2026-09-14 离线实验证据；语料为 SYNTHETIC/REDACTED 演示文档',
    purpose: '闭环能力证明：观测→评估→数据集→优化→留出回测的完整方法链与防过拟合纪律',
    risk_tags: ['检索质量', '防泄漏切分', 'held-out 回测', 'PROMOTE'],
    status: { verdict: 'PROMOTE', label: 'held-out MRR 0.861→0.958 · 晋级规则满足' },
    facts: {
      real_github_pr: { value: '否 —— 本地离线实验', ok: false, note: '' },
      real_agentteams_run: { value: '否 —— 真实检索器实验，但非 AgentTeams 运行', ok: false, note: '' },
      github_write: { value: '未写入 —— NOT_EXECUTED', ok: false, note: '' },
    },
    banner: null,
    stage_timeline: null,
    chain: ['观测 604 span→12 独立查询', '54 查询按族切分 30/24', '基线 MRR 0.793', '12 网格选中 bigram-idf-4096', 'held-out MRR 0.958 · PROMOTE'],
    generated_at: runMeta?.generated_at ?? '2026-09-14',
    source_dir: 'evidence/FINALS-RAG-LOOP-20260914（SHA256SUMS 锁定；timings 因非确定性排除）',
    honesty: HONESTY,
    steps,
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// --------------------------------------------------- case G: PR #2 SK5-TRACED run (sast_scan first call; shared SK5 session)

function caseGPr2Sk5Traced() {
  const K = 'finalsPr2Sk5Traced';
  if (!exists(K, 'kickoff-as-sent.txt')) return null;
  const rd = (f) => (exists(K, f) ? readText(K, f) : null);
  const reviewerResult = rd('tasks/pr2sk5-review-1/result.md');
  const reviewerFindings = rd('tasks/pr2sk5-review-1/workspace/findings.md');
  const fixerResult = rd('tasks/pr2sk5-fix-1/result.md');
  const fixerDiff = rd('tasks/pr2sk5-fix-1/attempt-1.diff');
  const fixerNotes = rd('tasks/pr2sk5-fix-1/workspace/notes.md');
  const verifierResult = rd('tasks/pr2sk5-verify-1/result.md');
  const verification = rd('tasks/pr2sk5-verify-1/workspace/verification.md');
  const gateApproval = rd('project/human-gate-approval.md');
  const gatePlan = rd('project/plan.md');
  const projectResult = rd('project/result.md');
  const kickoff = rd('kickoff-as-sent.txt');
  const ragAudit = rd('rag/rag-tool-spans.jsonl');
  const spanSummary = rd('span-summary.json');
  const usage = rd('usage-summary.json');
  const skillsModule = rd('image/zz_agentloop_skills.py');
  const mcpServer = rd('image/skill-mcp-server.mjs');
  const branches = rd('github-branches-after-sk5.txt');
  const teamRoom = exists(K, 'team-room-messages.json') ? readJson(K, 'team-room-messages.json') : null;
  const leaderDm = exists(K, 'leader-dm-messages.json') ? readJson(K, 'leader-dm-messages.json') : null;
  // Room exports are raw Matrix event lists spanning the whole room history; count the
  // SK5 PR2 window (16:51–16:58Z) live rather than copying numbers from anywhere.
  const evList = (x) => (Array.isArray(x) ? x : (x?.events ?? []));
  const winLo = Date.parse('2026-09-18T16:51:00Z');
  const winHi = Date.parse('2026-09-18T16:58:00Z');
  const winMsgs = (x) => evList(x).filter((e) => e?.type === 'm.room.message' && e.origin_server_ts >= winLo && e.origin_server_ts <= winHi).length;
  const rooms = {
    team_room: '!RErK7WVs9iUeaszwho:elemiso-matrix:6167',
    team_export_events: evList(teamRoom).length,
    team_window_messages: winMsgs(teamRoom),
    leader_dm: '!VocOLrDhUMBcBjzIuY:elemiso-matrix:6167',
    dm_export_events: evList(leaderDm).length,
    dm_window_messages: winMsgs(leaderDm),
    window: '2026-09-18T16:51Z–16:58Z（PR2 运行窗口）',
  };

  const RUN = 'run-elem-pr2sk5-20260919-01';
  const SHA = '1dedf5e1992c950557064d8f4fb9039d1523deb3';

  const H = {
    real: [
      '真实执行（SK5-TRACED）：非预设 SPEC、真实人工门（按操作员运行前书面授权自动投递）、批准后 12 秒 Leader 全新委派、Reviewer/Fixer/Verifier 真实调用确定性 skill_* 工具（tool.skill_* 独立 span + 宿主审计流水）',
      'SK5 会话四线并行：PR #2 批准路径 + PR #3 拒绝路径 + PR #1 低风险自动路径（NOT_CONFIRMED/LOW，无人工门，skill_sast_scan 首次被 Agent 调用并诚实识别 false positive）+ 双 Reviewer 信度实验（两独立容器同一 head 结论完全一致）；direct-probe 以 stdin 方式修复采集，4/4 非空',
    ],
    controlled: [
      '页面为只读回放；RAG 语料为知识型 SYNTHETIC（citation-only，无案例结论）；确定性 Skill 输出为 advisory（其 SKILL.md 自述 never an authorization decision）',
      'case_retrieval 的向量检索使用仓库自带的离线确定性 hash embedding（非语义模型）——排序为确定性向量相似度，升级语义检索属赛后项',
    ],
    not_executed: [
      '操作员对 Fixer 零执行指令介入；merge/push/close/reopen 未执行（PR #2 保持 OPEN，github-branches-after-sk5.txt 双分支 head 核验不变）',
      'usage-summary.json 初版打包误含 09-17 旧窗口（90 次/6.22M），2026-09-19 修正后与 SK5-MASTER-README 用量表同源对齐——本页数字均出自修正后的锁定工件',
      '直连探针本轮已修复采集（stdin 方式，4/4 非空）——此前四轮"未采集"的已知操作缺陷闭环',
    ],
  };

  const items = [
    {
      id: 'ev-sk-meta', title: 'CASE-MANIFEST（运行参数 + 技能/知识库能力声明，零结论预设）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['run_id / project', RUN + ' · elemiso-pr2sk5-gate'],
        ['head SHA', SHA],
        ['镜像', 'copaw-worker:223ddc2-agentloop-v3skills 系（构建文件随包：image/skill-mcp-server.mjs + image/zz_agentloop_skills.py）'],
        ['知识库与技能', 'manifest 明示确定性 Skill 可用（advisory-only）：diff_parse / risk_classify / case_retrieval（elemiso-case-pg 知识库）/ sast_scan；rag_retrieve 为组织规范库（SYNTHETIC 知识型语料，citation-only）'],
        ['kickoff', '16:51:32Z（kickoff-as-sent.txt 随包锁定；SPEC 不指定漏洞类型与定级）'],
        ['会话口径', 'SK5 四包（PR1/PR2/PR3/双评审）共享同一会话：span-summary 为会话累计，用量按 usage 工件分窗口；轮次总说明见 FINALS-ELEM-SK5-MASTER-README.md'],
      ],
      blocks: [{ title: 'finalsPr2Sk5Traced/kickoff-as-sent.txt', lang: 'text', text: kickoff ?? '未提供' }],
      source_ref: `${K}/kickoff-as-sent.txt`,
      hash: refHash(K, 'kickoff-as-sent.txt'),
      ...H,
    },
    {
      id: 'ev-sk-review', title: 'Reviewer 结论 pr2sk5-review-1（skill advisory 与自主定级分层）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['结论', 'FINDING_CONFIRMED · SEVERITY: HIGH · CWE-22 · HUMAN_VERIFICATION_REQUIRED: YES'],
        ['范围与复现', 'git diff 4cd5bf0..HEAD = 2 files +122/−0；自写 PoC：../ 逃逸 → 200 泄露；/etc/hostname 绝对路径 → 200 任意读'],
        ['skill_diff_parse', 'req-c6c8c35c94 → 2 files +122/−0（categories source+test）'],
        ['skill_risk_classify', 'req-342c5a4f3b → 建议性 L1（SOURCE_CONFIG_CHANGE）+ HUMAN_REVIEW 控制 —— advisory 不覆盖自主定级：独立代码审查 + 复现确立 HIGH'],
        ['事件', '委派 16:51:38Z $fDnBNKqruc_A_BxrD60EYHp7VE2xdlR-csNo_BMO0oI · 提交 16:53:48Z（kickoff 后 2 分 06 秒）'],
      ],
      blocks: [
        { title: 'finalsPr2Sk5Traced/tasks/pr2sk5-review-1/result.md', lang: 'text', text: reviewerResult ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/tasks/pr2sk5-review-1/workspace/findings.md', lang: 'text', text: reviewerFindings ?? '未提供' },
      ],
      source_ref: `${K}/tasks/pr2sk5-review-1/result.md`,
      hash: refHash(K, 'tasks/pr2sk5-review-1/result.md'),
      ...H,
    },
    {
      id: 'ev-sk-gate', title: '人工安全门（按操作员运行前授权自动投递：批准）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['Leader 行为', '审查验收后停门（团队房向 Team Admin 报告并请求门决策；fix/verify 计划锁定待决）'],
        ['操作员决策', 'APPROVED（16:54:48Z，交互式门提示，操作员在场响应；决策等待 1 分 00 秒为操作员时间，如实记录）'],
        ['批准记录', '先于派发落盘 project/human-gate-approval.md；本轮包未含 gate-approval-sent 快照，DM/团队房事件号见房间导出'],
      ],
      blocks: [
        { title: 'finalsPr2Sk5Traced/project/human-gate-approval.md', lang: 'text', text: gateApproval ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/project/plan.md（批准后的 plan 快照）', lang: 'text', text: gatePlan ?? '未提供' },
      ],
      source_ref: `${K}/project/human-gate-approval.md`,
      hash: refHash(K, 'project/human-gate-approval.md'),
      ...H,
    },
    {
      id: 'ev-sk-fix', title: 'Fixer 补丁 pr2sk5-fix-1（skill 辅助下的确定性修复）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['关键验收', '门批准后 12 秒 Leader 发出全新委派事件 $3xgqje5vDux1KpHCr0G6z8hk8I9v93R4Pw3l_xeVXks（16:55:00Z 委派）'],
        ['交付', 'attempt-1.diff 单文件 +13/−7；sha256 674356fc…16081 —— 与此前各轮独立产出逐字节一致（本轮第八次）；提交 16:55:30Z（委派后 30 秒）'],
      ],
      blocks: [
        { title: 'finalsPr2Sk5Traced/tasks/pr2sk5-fix-1/attempt-1.diff', lang: 'diff', text: fixerDiff ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/tasks/pr2sk5-fix-1/result.md', lang: 'text', text: fixerResult ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/tasks/pr2sk5-fix-1/workspace/notes.md', lang: 'text', text: fixerNotes ?? '未提供' },
      ],
      source_ref: `${K}/tasks/pr2sk5-fix-1/attempt-1.diff`,
      hash: refHash(K, 'tasks/pr2sk5-fix-1/attempt-1.diff'),
      ...H,
    },
    {
      id: 'ev-sk-verify', title: 'Verifier 独立验证（VERIFIED PASS，首次通过）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['独立工作区', '干净 clone @ head SHA；git apply 干净通过；sha256 独立复算一致；backend/tests/ 未触碰'],
        ['修复前', 'pristine checkout 上 3 逃逸向量全 200 泄露（含 ../../../../../../etc/hostname = 158f57ca9296）；缺失 500'],
        ['修复后', '越权全 400 Invalid file path · 合法 ok.txt 200 · 缺失 404 · py_compile PASS；PR 测试断言反转如实记录'],
        ['skill 交叉核对', 'skill_diff_parse ×1 + skill_risk_classify ×1（建议性 L1，低估 vs 自主 HIGH）——advisory 输出不替代自主复现'],
        ['事件', '委派 16:55:44Z $0PQZDxKql3Aj4Q1JrD2OQnM6ZxgUORqvmF4IZPqjGI0 · 提交 VERIFIED 16:56:46Z（房间 16:56:50 报告）'],
      ],
      blocks: [
        { title: 'finalsPr2Sk5Traced/tasks/pr2sk5-verify-1/result.md', lang: 'text', text: verifierResult ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/tasks/pr2sk5-verify-1/workspace/verification.md', lang: 'markdown', text: verification ?? '未提供' },
      ],
      source_ref: `${K}/tasks/pr2sk5-verify-1/result.md`,
      hash: refHash(K, 'tasks/pr2sk5-verify-1/result.md'),
      ...H,
    },
    {
      id: 'ev-sk-run', title: '运行档案（用量/span/合规）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['墙钟', 'kickoff 16:51:32Z → 项目 completed 16:57:03Z = 5 分 31 秒（含 Reviewer 依赖安装；门决策等待 1 分 00 秒为操作员时间，如实分列）'],
        ['用量（锁定工件 pr2 窗口）', '86 次调用 · 输入 15,643,868（缓存 14,554,752 ≈93%）· 输出 36,522；四案例窗口合计 195 次 / 输入 26,786,541 / 输出 65,032（四包同会话，窗口外会话开销不计入）'],
        ['追踪（会话累计）', '锁定 span-summary：824 span（leader 280 / reviewer 240 / fixer 118 / verifier 129 / reviewer-b 57）· tool.rag_retrieve ×7 · agentteams.delegation.link ×15 · skill_* ×15（reviewer 13 + verifier 2）'],
        ['合规', 'PR #2 全程 OPEN · github-branches-after-sk5.txt：两分支 head 运行后与记录一致 · 零 GitHub 写入'],
      ],
      blocks: [
        { title: 'finalsPr2Sk5Traced/project/result.md（项目终报）', lang: 'markdown', text: projectResult ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/usage-summary.json（分窗口用量）', lang: 'json', text: usage ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/span-summary.json（SK5 会话累计）', lang: 'json', text: spanSummary ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/github-branches-after-sk5.txt', lang: 'text', text: branches ?? '未提供' },
        { title: 'finalsPr2Sk5Traced/rag/rag-tool-spans.jsonl（服务端审计流水，跨轮累积）', lang: 'json', text: ragAudit ?? '未提供' },
      ],
      source_ref: `${K}/project/result.md`,
      hash: refHash(K, 'project/result.md'),
      ...H,
    },
    {
      id: 'ev-sk-skills', title: 'Skills MCP（确定性技能集 · 全程留痕）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['技能集', 'skill_diff_parse · skill_risk_classify（advisory L1）· skill_case_retrieval（elemiso-case-pg 知识库）· skill_sast_scan（静态扫描）· skill_test_runner'],
        ['sast_scan 首战', '低风险 PR1 路径（同会话）reviewer 对 bootstrap 文件跑 SAST：tool.skill_sast_scan ×2；严格解析器拒绝手写重构 diff 后改用文件原文——诚实识别测试文件中 subprocess.run 为 false positive（非 shell=True）'],
        ['分层语义', '工具输出 advisory-only：建议性 L1 < Agent 自主定级（HIGH）< 人工门；SAST 命中不等于漏洞结论——false positive 如实入档'],
        ['构建', 'image/skill-mcp-server.mjs + image/zz_agentloop_skills.py 入包'],
      ],
      blocks: [
        { title: 'finalsPr2Sk5Traced/image/skill-mcp-server.mjs（前 3000 字；全文见证据目录）', lang: 'javascript', text: mcpServer ? mcpServer.slice(0, 3000) + '\n…' : '未提供' },
        { title: 'finalsPr2Sk5Traced/image/zz_agentloop_skills.py（前 3000 字）', lang: 'python', text: skillsModule ? skillsModule.slice(0, 3000) + '\n…' : '未提供' },
      ],
      source_ref: `${K}/image/skill-mcp-server.mjs`,
      hash: refHash(K, 'image/skill-mcp-server.mjs'),
      ...H,
    },
    {
      id: 'ev-sk-rooms', title: 'Matrix 房间导出（全量事件 + 运行窗口实时计数）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE', postgate: true,
      fields: [
        ['团队房', `${rooms.team_room}（导出 ${rooms.team_export_events} 事件，跨 09-16/09-17/09-18 多轮房史；SK5 PR2 窗口 16:51–16:58Z 消息 ${rooms.team_window_messages} 条）`],
        ['Leader DM', `${rooms.leader_dm}（导出 ${rooms.dm_export_events} 事件；SK5 PR2 窗口消息 ${rooms.dm_window_messages} 条）`],
      ],
      blocks: [{ title: 'finalsPr2Sk5Traced/team-room-messages.json + leader-dm-messages.json（实时计数）', lang: 'json', text: JSON.stringify(rooms, null, 2) }],
      source_ref: `${K}/team-room-messages.json`,
      hash: refHash(K, 'team-room-messages.json'),
      ...H,
    },
  ];

  const steps = [
    {
      id: 'pr-init', title: '① PR 发起', role: 'Leader 编排', time: '16:51:32Z kickoff',
      headline: '非预设 SPEC 下发任务——manifest 声明确定性 Skill 与知识库可用（advisory-only），零结论预设',
      points: [
        { k: 'PR 内容', v: 'backend/src/interfaces/api/v1/demo_high_risk.py（PR #2 变更文件 · 待审对象）' },
        { k: 'run_id', v: RUN, mono: true },
        { k: '知识库与技能', v: 'rag_retrieve（citation-only）+ 确定性 skill_*：diff_parse / risk_classify / case_retrieval / sast_scan（advisory-only）' },
        { k: '会话', v: 'SK5 四包同会话：本案例 + PR #3 拒绝 + PR #1 低风险自动 + 双 Reviewer 信度实验', },
        { k: 'head SHA', v: SHA, mono: true },
      ],
      evidence: ['ev-sk-meta'], probe: null,
      exhibit: {
        type: 'terminal',
        title: '下发给 Leader 的任务书 · kickoff-as-sent.txt 摘录（省略处以 … 标注，Enter 看逐字全文）',
        lines: [
          { text: '@leader:elemiso-matrix:6167 [kickoff 16:51:32Z] run-elem-pr2sk5-20260919-01 — project elemiso-pr2sk5-gate', tone: 'dim' },
          { text: 'You are the Team Leader. The project DAG already exists. Do NOT create projects or tasks beyond it; do NOT replan.' },
          { text: '4. HUMAN SECURITY GATE: if the Reviewer result contains any HIGH/CRITICAL confirmed finding or HUMAN_VERIFICATION_REQUIRED: YES — do NOT delegate anything … WAIT for my explicit decision … If REJECT: never delegate; fix [-] rejected · verify [!] locked · project blocked', tone: 'amber' },
          { text: '=== SPEC REVIEW (pr2sk5-review-1 → @reviewer) ===', tone: 'dim' },
          { text: '… review ALL files added by this PR … produce your own independent findings — your own severity and CWE rating, one reproducible command, real output summary', tone: 'ok' },
          { text: 'do NOT modify repository files; do NOT include fix/patch code; zero GitHub writes' },
          { text: '—— SPEC 全文未指定漏洞类型：找什么、怎么定级，都是 Reviewer 自己的判断 ——', tone: 'dim' },
        ],
      },
      exhibit_pending: {
        type: 'terminal',
        title: '下发给 Leader 的任务书 · kickoff-as-sent.txt 摘录（省略处以 … 标注，Enter 看逐字全文）',
        lines: [
          { text: '@leader:elemiso-matrix:6167 [kickoff 16:51:32Z] run-elem-pr2sk5-20260919-01 — project elemiso-pr2sk5-gate', tone: 'dim' },
          { text: 'You are the Team Leader. The project DAG already exists. Do NOT create projects or tasks beyond it; do NOT replan.' },
          { text: '4. HUMAN SECURITY GATE: if the Reviewer result contains any HIGH/CRITICAL confirmed finding or HUMAN_VERIFICATION_REQUIRED: YES — do NOT delegate anything … WAIT for my explicit decision …', tone: 'amber' },
          { text: '=== SPEC REVIEW (pr2sk5-review-1 → @reviewer) ===', tone: 'dim' },
          { text: '… review ALL files added by this PR … produce your own independent findings — your own severity and CWE rating, one reproducible command, real output summary', tone: 'ok' },
          { text: 'do NOT modify repository files; do NOT include fix/patch code; zero GitHub writes' },
          { text: '—— SPEC 全文未指定漏洞类型：找什么、怎么定级，都是 Reviewer 自己的判断 ——', tone: 'dim' },
        ],
      },
      detail: { title: '运行环境', quote: 'elemiso 隔离栈；kickoff-as-sent.txt 随包 SHA256 锁定；轮次总说明见 FINALS-ELEM-SK5-MASTER-README.md。', outbox: [], events: [] },
    },
    {
      id: 'review', title: '② 风险审查（非预设 SPEC）', role: 'Reviewer', time: '16:51:38Z → 16:53:48Z · 2 分 06 秒',
      headline: 'Reviewer 自主复现确认 CWE-22——确定性 Skill 只给建议，定级归 Agent', stat: '确认 HIGH · CWE-22',
      points: [
        { k: 'Reviewer 结论', v: 'FINDING_CONFIRMED · SEVERITY: HIGH · CWE-22' },
        { k: '独立复现', v: '自写 PoC（../ 逃逸泄露 + /etc/hostname 任意读）' },
        { k: 'skill 分层', v: 'skill_diff_parse 佐证范围（2 files +122/−0）；skill_risk_classify 建议性 L1——advisory 不覆盖自主 HIGH' },
        { k: '时效（如实）', v: '委派 → 提交 2 分 06 秒，含依赖安装等待（各轮耗时差异如实分列，不做归因）' },
      ],
      evidence: ['ev-sk-review'], probe: null,
      exhibit: {
        type: 'terminal',
        title: 'Reviewer 独立 PoC · pr2sk5-review-repro.py（findings.md 原文输出）',
        lines: [
          { text: '$ /opt/venv/standard/bin/python pr2sk5-review-repro.py', tone: 'cmd' },
          { text: '[+] HTTP status       : 200', tone: 'bad' },
          { text: "[+] body              : 'TOP-SECRET-OUTSIDE-BASE'", tone: 'bad' },
          { text: '[+] escaped base?     : True', tone: 'bad' },
          { text: '[+] LEAK CONFIRMED    : True', tone: 'bad' },
          { text: '[*] arbitrary read    : /etc/hostname → 200（宿主文件内容回显）', tone: 'bad' },
        ],
      },
      detail: { title: '检索纪律', quote: 'rag_retrieve 引用组织规范（references only）；skill_risk_classify 的建议性 L1 与自主 HIGH 并存——结论出自自主复现，不出自工具。', outbox: [], events: [] },
    },
    {
      id: 'rag', title: '③ 知识与技能（RAG MCP + Skills MCP）', role: 'Reviewer · Fixer · Verifier', role_pending: 'Reviewer',
      time: 'skill_* ×15 · 会话累计', time_pending: '审查期内',
      headline: '确定性 Skill 全家桶实战——sast_scan 首战诚实识别 false positive，检索结果只是 untrusted 引用',
      headline_pending: '确定性 Skill 与知识库接入——建议是 advisory，引用不替代验证',
      stat: 'skill ×15 · 全程留痕', stat_pending: 'advisory 工具 · 全程留痕',
      hand: 'RAG MCP · citation-only · 检索审计落盘', hand_pending: 'RAG MCP · citation-only · 检索审计落盘',
      points: [
        { k: '本轮增量', v: 'skill_sast_scan 首次被 Agent 调用（低风险 PR1 路径，同会话）：严格解析器拒绝手写重构 diff 后改用文件原文——诚实识别 subprocess.run 命中为 false positive（非 shell=True）' },
        { k: '分层语义', v: '工具建议（advisory L1）< Agent 自主定级（HIGH）< 人工门——三层可靠性不因工具存在而被绕过' },
        { k: 'RAG 引用', v: 'rag_retrieve ×7（reviewer 5 / fixer 1 / verifier 1，锁定 span-summary）：citation-only，语料零案例结论，服务端只存 query_hash' },
        { k: '留痕', v: 'tool.skill_* 独立 span（直连 SLS）+ 宿主审计流水；case_retrieval 持续产出（reviewer ×2，知识库引用 untrusted）' },
      ],
      points_pending: [
        { k: '谁在用', v: 'Reviewer / Verifier 按各自任务需要独立调用' },
        { k: '分层语义', v: '工具建议（advisory L1）< Agent 自主定级（HIGH）< 人工门——三层可靠性不因工具存在而被绕过' },
        { k: 'RAG 引用', v: 'rag_retrieve citation-only，语料零案例结论，服务端只存 query_hash' },
        { k: '留痕', v: 'tool.skill_* 独立 span（直连 SLS）+ 宿主审计流水' },
      ],
      evidence: ['ev-sk-skills'], probe: null,
      exhibit: {
        type: 'terminal',
        title: 'SAST 首战 · 会话内 reviewer 房间实录（低风险 PR1 路径）',
        lines: [
          { text: 'Now run SAST scan on the changed files.', tone: 'cmd' },
          { text: 'Paths mode unavailable; use inline mode.', tone: 'dim' },
          { text: 'The strict parser rejects blank context lines in my hand-reconstructed diff.', tone: 'dim' },
          { text: '  → use the exact file contents', tone: 'dim' },
          { text: 'SAST 命中：测试文件 subprocess.run —— 诚实识别为 false positive（非 shell=True）', tone: 'ok' },
        ],
      },
      exhibit_pending: {
        type: 'terminal',
        title: '审计与留痕',
        lines: [
          { text: 'tool.skill_* / rag_retrieve 全程独立 span 留痕（直连 SLS）', tone: 'dim' },
          { text: '宿主审计流水与分窗口用量见运行档案', tone: 'dim' },
        ],
      },
      detail: { title: '审计口径', quote: 'SK5 四包共享同一会话：span-summary 为会话累计（824 span，含 PR1/PR3/双评审），用量按 usage 工件分窗口——口径在案，不混计为本案例独占。', outbox: [], events: [] },
      detail_pending: { title: '审计口径', quote: 'span 为 SK5 会话累计口径；分角色与分窗口明细见运行档案。', outbox: [], events: [] },
    },
    {
      id: 'gate', title: '④ 人工安全门（批准）', title_pending: '④ 人工安全门', role: '人工决策', time: '16:54:48Z 批准', tone: 'gate',
      headline: 'HIGH 触发全系统停等——批准后 12 秒 Leader 发出全新委派', points: [
        { k: 'Leader 行为', v: '审查验收后停门（团队房向 Team Admin 报告并请求门决策；fix/verify 计划锁定待决）' },
        { k: '操作员决策', v: 'APPROVED（16:54:48Z，交互式门提示，操作员在场响应；决策等待 1 分 00 秒为操作员时间，如实记录）' },
      ],
      evidence: ['ev-sk-gate'], probe: null,
      exhibit: {
        type: 'ticket',
        title: '人工安全门批准记录 · project/human-gate-approval.md（先于派发落盘）',
        stamp: 'APPROVED',
        rows: [
          ['决策时间', '16:54:48Z（UTC）—— 交互式门提示，操作员在场响应'],
          ['批准人', 'repository / workspace owner（human operator）'],
          ['审查对象', 'pr2sk5-review-1 · head SHA 1dedf5e1…deb3（PR #2 已核验）'],
          ['授权范围', '委派 pr2sk5-fix-1（最小修复 · 测试冻结 · 零 GitHub 写入）→ verify-1；FAIL→fix-2 一次；二次 FAIL→停、block、升级'],
        ],
        foot: '验收判据（新委派事件先于一切 Fixer 动作）记录于批准文件；本轮包未含 gate-approval-sent 快照，事件号见房间导出',
      },
      detail: { title: '授权口径', quote: '授权范围与先例一致：最小修复+测试冻结+零 GitHub 写入；FAIL 一次重派。', outbox: [], events: [] },
    },
    {
      id: 'fix-diff', title: '⑤ 修复 diff（确定性产出）', role: 'Fixer', time: '16:55:00Z → 16:55:30Z',
      headline: '确定性修复——八轮独立产出 sha256 逐字节一致', points: [
        { k: '关键验收', v: '门后全新委派事件 $3xgqje5vDux1KpHC…（批准后 12 秒 · 16:55:00Z 委派）', mono: true },
        { k: '交付物', v: 'attempt-1.diff 单文件 +13/−7；sha256 674356fc…16081', mono: true },
        { k: '时效', v: '委派后 30 秒提交（16:55:30Z）——最小修复路径稳定复现' },
      ],
      evidence: ['ev-sk-fix'], probe: null,
      detail: { title: 'fixer 执行', quote: 'Fixer 按契约在新目录 clone/修改/diff/sha256/提交；无操作员执行指令。', outbox: [], events: [] },
    },
    {
      id: 'verify', title: '⑥ 独立验证（VERIFIED PASS）', title_pending: '⑥ 独立验证', role: 'Verifier', time: '16:55:44Z → 16:56:46Z',
      headline: 'Verifier 不采信 Fixer 自述——独立工作区复测，首次即 VERIFIED', points: [
        { k: '独立工作区', v: '干净 clone @ head SHA；git apply 干净通过；sha256 独立复算一致；backend/tests/ 未触碰' },
        { k: '修复前', v: '3 逃逸向量 200+泄露（含 ../../../../../../etc/hostname = 158f57ca9296）/ 缺失 500' },
        { k: '修复后', v: '越权全 400 / 合法 200 / 缺失 404 / py_compile PASS' },
        { k: 'skill 交叉核对', v: 'skill_diff_parse ×1 + skill_risk_classify ×1（建议性 L1）——advisory 输出不替代自主复现' },
      ],
      evidence: ['ev-sk-verify'],
      probe: {
        headline: '探针对照 · Verifier 独立干净工作区实测（verification.md §A/§B 原文）',
        rows: [
          { label: '合法基内文件', request: 'name=ok.txt', before: '200 · LEGIT-INSIDE-BASE', after: '200 · LEGIT-INSIDE-BASE', kind: 'stable' },
          { label: '相对 ../ 穿越', request: 'name=../outside/outside-secret.txt', before: '200 · 泄露 TOP-SECRET-OUTSIDE-BASE', after: '400 · Invalid file path', kind: 'fixed' },
          { label: '子目录嵌套 ../', request: 'name=sub/../../outside/outside-secret.txt', before: '200 · 泄露 TOP-SECRET-OUTSIDE-BASE', after: '400 · Invalid file path', kind: 'fixed' },
          { label: '绝对目标穿越', request: 'name=../../../../../../etc/hostname', before: '200 · 泄露 \'158f57ca9296\'', after: '400 · Invalid file path', kind: 'fixed' },
          { label: '基内缺失文件', request: 'name=does-not-exist.txt', before: '500 · Internal Server Error', after: '404 · File not found', kind: 'fixed' },
        ],
      },
      detail: { title: '验证事件链', quote: '只采信自己的 clone、apply、探针状态码（16:56:46Z VERIFIED，房间 16:56:50 报告）。', outbox: [], events: [] },
    },
    {
      id: 'approval', title: '⑦ Leader 验收 · 最终处置', role: 'Leader', time: '16:57:03Z 终报',
      headline: '三任务全验收、项目 completed——PR #2 保持 OPEN，零 GitHub 写入', points: [
        { k: 'Leader 验收', v: '三任务全验收，plan 全 [x]，项目 completed' },
        { k: '终报', v: '向 Team Admin 报告最终结果（16:57:03Z，墙钟 5 分 31 秒）' },
        { k: 'PR 状态', v: 'PR #2 保持 OPEN · 零 GitHub 写入（github-branches-after-sk5.txt 双分支 head 不变）' },
      ],
      evidence: ['ev-sk-run', 'ev-sk-skills', 'ev-sk-rooms'], probe: null,
      exhibit: {
        type: 'checklist',
        title: 'Leader 验收清单 · TaskResult ×3（plan 全 [x]）',
        items: [
          { label: 'pr2sk5-review-1', sub: 'FINDING_CONFIRMED · HIGH · CWE-22 —— 验收' },
          { label: 'pr2sk5-fix-1', sub: 'attempt-1.diff +13/−7 · sha256 独立复算一致 —— 验收' },
          { label: 'pr2sk5-verify-1', sub: 'VERIFIED PASS · 独立干净工作区 —— 验收' },
        ],
        foot: '终报 16:57:03Z · PR #2 保持 OPEN · 零 GitHub 写入',
      },
      decision: {
        verified: { verdict: 'PASS', text: 'VERDICT: VERIFIED —— 门后新委派事件 + 独立验证通过 + 全程 v3/Skills 追踪' },
        human_approval: { needed: true, text: '需要且已执行 —— 按操作员运行前授权自动投递（批准）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        merge_allowed: { allowed: false, text: 'PR #2 保持 OPEN —— 合并未执行' },
        github_write: { done: false, text: '未写入 GitHub', level: 'NOT_EXECUTED' },
        execution_nature: { text: 'REAL_EXECUTED-AgentTeams（确定性 Skill 实战）：非预设 SPEC + 干净派发链 + 全程追踪', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        pr_open: true,
      },
      detail: { title: '终态', quote: '运行结束计费调用者全部停止；SK4/SK3/V3 等前序轮次证据保留为对照。', outbox: [], events: [] },
    },
    {
      id: 'trace', title: '⑧ 追踪与观测', role: 'AgentLoop', time: '会话累计 · 824 span', stat: '824 span · 会话口径',
      headline: '824 span 会话级追踪——过程轨迹与结果同样可审计', points: [
        { k: '轨迹分解', v: 'span 按角色（锁定 span-summary，会话累计）：leader 280 · reviewer 240 · fixer 118 · verifier 129 · reviewer-b 57' },
        { k: '工具调用', v: 'skill_* ×15（reviewer 13：diff_parse 3 / risk_classify 6 / sast_scan 2 / case_retrieval 2；verifier 2）· rag ×7 · delegation.link ×15' },
        { k: '口径', v: '四包同会话：span 为 SK5 会话累计，用量按 usage 工件分窗口（pr2/pr3/session 三口径并列）——不混计为单案例独占' },
        { k: '披露', v: 'direct-probe 本轮修复采集（stdin 方式，4/4 非空 375–377 字节）——此前四轮未采集的已知操作缺陷闭环' },
      ],
      evidence: ['ev-sk-skills', 'ev-sk-run'], probe: null,
      exhibit: {
        type: 'bars',
        title: '824 span · 按角色分解（span-summary.json · SK5 会话累计）',
        rows: [
          { label: 'leader', value: 280 },
          { label: 'reviewer', value: 240 },
          { label: 'verifier', value: 129 },
          { label: 'fixer', value: 118 },
          { label: 'reviewer-b', value: 57 },
        ],
        foot: 'skill_* ×15 · rag ×7 · delegation.link ×15 · 四包同会话（含双 Reviewer 实验）',
      },
      detail: { title: '构建来源', quote: 'image/skill-mcp-server.mjs + zz_agentloop_skills.py 入包；delegation.link 数据层验证：LINK_SPAN_TRACE trace_id 与 parent_trace 一致。', outbox: [], events: [] },
    },
  ];

  return {
    case_id: 'fastapi-pr2-rag-traced-20260917',
    shape: 'guided',
    name: 'FastAPI PR #2 · 高风险变更 · 人工批准闭环',
    name_pending: 'FastAPI PR #2 · 高风险变更审修',
    short_name: 'PR #2 · 批准路径',
    one_liner: '非预设 SPEC 下 Reviewer 自主确认 HIGH（CWE-22，skill_risk_classify 建议性 L1 未覆盖自主定级）→ 人工门批准（授权自动投递）→ Leader 12 秒全新委派 → Fixer 确定性修复（sha256 八轮一致）→ Verifier VERIFIED；墙钟 5 分 31 秒，direct-probe 4/4 采集成功。',
    repo: 'nghqqa/fastapi-boilerplate-demo',
    pr: 'PR #2 · demo/high-risk-human-gate',
    pending_redactions: [
      ['demo/high-risk-human-gate', 'demo/···'],
      ['copaw-high-risk-human-gate', 'copaw-high-risk-···'],
      ['sast_scan 首战 + case_retrieval 持续产出', '核心增量：确定性 Skill 全家桶接入'],
      ['Fixer 补丁 pr2sk5-fix-1（skill 辅助下的确定性修复）', '门后任务产物（决策后可见）'],
      ['Verifier 独立验证（VERIFIED PASS，首次通过）', '门后任务产物（决策后可见）'],
      ['人工安全门（按操作员运行前授权自动投递：批准）', '人工安全门 · 决策记录'],
    ],
    pr_url: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/2',
    run_id: RUN,
    sha: SHA,
    sha_kind: 'PR #2 head commit · ls-remote 已核验',
    evidence_level: ['REAL_EXECUTED_AGENTTEAMS_LIVE'],
    replay_note: 'REAL_EXECUTED —— 本页面回放该次运行的证据；不连接实时系统',
    purpose: '主案例：高风险变更的独立审查、人工授权修复与独立验证——advisory 工具与历史案例引用不覆盖自主判断，自主判断不越过人工门',
    risk_tags: ['CWE-22 路径穿越', '任意文件读取', 'HIGH', '确定性 Skill MCP', 'sast_scan', '案例知识库', 'AgentLoop v3 追踪'],
    status: { verdict: 'PASS', label: 'VERIFIED PASS · PR 保持 OPEN' },
    pitch_next: { to: '/demo/fastapi-pr3-reject?stage=1', kicker: '下一幕', label: '同一道门 · 第二个 PR' },
    gate_alt: { label: 'PR #3 · 同一门的拒绝路径', path: '/demo/fastapi-pr3-reject' },
    stats: [
      { k: '端到端墙钟', v: '5 分 31 秒', hint: '含 Reviewer 依赖安装与门决策等待 1 分 00 秒（操作员时间），如实分列' },
      { k: 'LLM 调用', v: '86 次', hint: 'pr2 窗口 · 输入 15.64M · ≈93% 命中缓存' },
      { k: '追踪 span', v: '824 · 会话累计', hint: 'SK5 四包同会话（含 PR1/PR3/双评审）' },
      { k: '确定性 Skill', v: '×15 · 会话累计', hint: 'diff_parse/risk_classify/sast_scan/case_retrieval · advisory-only' },
      { k: '补丁一致性', v: 'sha256 八轮一致', hint: '674356fc…16081' },
    ],
    facts: {
      real_github_pr: { value: '真实 GitHub PR（OPEN）', ok: true, note: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/2' },
      real_agentteams_run: { value: '真实 AgentTeams 运行（SK5-TRACED）', ok: true, note: '门后全新委派事件；三角色真实调用 skill_* 工具；delegation.link ×15（会话）' },
      skills_mcp: { value: '确定性 Skill + sast_scan 首战（advisory-only）', ok: true, note: 'SAST 命中诚实识别 false positive；建议性 L1 未覆盖自主 HIGH' },
      github_write: { value: '未写入 —— PR 保持 OPEN', ok: false, note: 'merge/push/close/reopen 全程禁止' },
    },
    banner: null,
    stage_timeline: null,
    chain: ['案例 ' + RUN, 'PR #2', 'commit ' + short(SHA, 10), 'demo_high_risk.py', 'attempt-1.diff', 'probe 200→400', 'VERIFIED PASS'],
    generated_at: '运行窗口 16:51Z–16:57Z（墙钟 5 分 31 秒，含人工门决策等待 1 分 00 秒）',
    source_dir: 'evidence/FINALS-ELEM-PR2-SK5-TRACED（SHA256SUMS 锁定，54 清单项）',
    honesty: H,
    steps,
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// --------------------------------------------------- case I: PR #1 low-risk auto path (SK5)

function caseIPr1Sk5Auto() {
  const K = 'finalsPr1Sk5Auto';
  if (!exists(K, 'tasks/pr1sk5-review-1/meta.json')) return null;
  const rd = (f) => (exists(K, f) ? readText(K, f) : null);
  const reviewerResult = rd('tasks/pr1sk5-review-1/result.md');
  const reviewerFindings = rd('tasks/pr1sk5-review-1/workspace/findings.md');
  const plan = rd('project/plan.md');
  const projectResult = rd('project/result.md');
  const usage = rd('usage-summary.json');
  const spanSummary = rd('span-summary.json');
  const branches = rd('github-branches-after-sk5.txt');

  const RUN = 'run-elem-pr1sk5-20260919-01';
  const SHA = '4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c';

  const H = {
    real: [
      '真实执行（低风险自动路径）：NOT_CONFIRMED / LOW → 单节点 DAG 审查即结、项目自动 completed，无人工门——按风险分级设计，非跳过',
      'skill_sast_scan 首次被 Agent 调用：唯一命中为测试文件 subprocess.run（argv 列表、非 shell=True），Agent 诚实裁决 false positive 并留痕',
    ],
    controlled: [
      '页面为只读回放；确定性 Skill 输出为 advisory（其 SKILL.md 自述 never an authorization decision）',
      'case_retrieval 检索使用离线确定性 hash embedding（非语义模型）——本轮如实返回空匹配（无相关历史案例）',
    ],
    not_executed: [
      '无人工门——NOT_CONFIRMED/LOW 未触发停等条件（设计行为）；无修复/验证对象，Fixer/Verifier 未派发',
      '零 GitHub 写入；PR #1 保持 OPEN',
      'usage 工件初版未单列 pr1 窗口（曾以估计值并列披露）；2026-09-19 修正后已单列精确窗口：45 次 / 输入 5,084,301（99% 缓存）/ 输出 14,661',
    ],
  };

  const items = [
    {
      id: 'ev-auto-meta', title: 'CASE-MANIFEST（低风险自动路径：单节点 DAG + 技能能力声明）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['run_id / project', RUN + ' · elemiso-pr1sk5-auto'],
        ['head SHA', SHA + '（PR #1 bootstrap head · base fdde4f41…）'],
        ['DAG', '单节点 pr1sk5-review-1——无 fix/verify/人工门（低风险按分级设计）'],
        ['知识库与技能', 'manifest 明示确定性 Skill 可用（advisory-only）：diff_parse / risk_classify / sast_scan / case_retrieval；rag_retrieve（citation-only）'],
        ['锁定边界', 'SK5 包 SHA256SUMS 锁定 39 项；kickoff 快照未随包，建项（17:04:12Z）与房间导出留痕'],
      ],
      blocks: [{ title: 'finalsPr1Sk5Auto/project/plan.md（单节点 DAG 计划）', lang: 'markdown', text: plan ?? '未提供' }],
      source_ref: `${K}/project/plan.md`,
      hash: refHash(K, 'project/plan.md'),
      ...H,
    },
    {
      id: 'ev-auto-review', title: 'Reviewer 结论 pr1sk5-review-1（NOT_CONFIRMED · LOW）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['结论', 'STATUS: NOT_CONFIRMED · SEVERITY: LOW · HUMAN_VERIFICATION_REQUIRED: NO——未引入新的安全漏洞'],
        ['范围', 'git diff --stat = 6 files +168/−1（PR_DESCRIPTION.md 等）· head 4cd5bf0 git rev-parse 核验'],
        ['skill_sast_scan', 'req-c2c37caad4（inline 模式）：唯一命中 = 测试文件 subprocess.run（argv 列表、常量参数、非 shell=True）→ 裁决 false positive；paths 模式返回 SAST_SCAN_TRUSTED_CONFIG_MISSING（需部署根配置）→ 如实降级 inline'],
        ['skill_case_retrieval', '无相关历史案例（知识库仅 PR #2/#3 案例）——如实返回空匹配，不以近似案例凑数'],
        ['事件', '委派 17:05:18Z $BD0lIZpUswA11CBPc9OUdVMaK6hK-GWyKRNnLN2f8SA · 提交 17:06:25Z（67 秒）'],
      ],
      blocks: [
        { title: 'finalsPr1Sk5Auto/tasks/pr1sk5-review-1/result.md', lang: 'text', text: reviewerResult ?? '未提供' },
        { title: 'finalsPr1Sk5Auto/tasks/pr1sk5-review-1/workspace/findings.md', lang: 'text', text: reviewerFindings ?? '未提供' },
      ],
      source_ref: `${K}/tasks/pr1sk5-review-1/result.md`,
      hash: refHash(K, 'tasks/pr1sk5-review-1/result.md'),
      ...H,
    },
    {
      id: 'ev-auto-run', title: '运行档案（自动闭环与会话口径）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE',
      fields: [
        ['自动闭环', '审查提交 → 单节点 DAG 即结 → 项目 completed（17:06:38Z 团队房留痕「low-risk auto path」）→ 17:06:44Z 综合终报'],
        ['用量（pr1 窗口）', 'usage-summary.json 单列 pr1sk5：45 次调用 · 输入 5,084,301（缓存 5,060,096 ≈99%）· 输出 14,661'],
        ['追踪（会话累计）', 'SK5 四包同会话：span-summary 824 span（本案例为其中 reviewer 段）· skill_sast_scan ×2 独立 span'],
        ['合规', 'PR #1 全程 OPEN · github-branches-after-sk5.txt：两分支 head 运行后与记录一致 · 零 GitHub 写入'],
      ],
      blocks: [
        { title: 'finalsPr1Sk5Auto/project/result.md（项目终报）', lang: 'markdown', text: projectResult ?? '未提供' },
        { title: 'finalsPr1Sk5Auto/usage-summary.json（分窗口用量）', lang: 'json', text: usage ?? '未提供' },
        { title: 'finalsPr1Sk5Auto/span-summary.json（SK5 会话累计）', lang: 'json', text: spanSummary ?? '未提供' },
        { title: 'finalsPr1Sk5Auto/github-branches-after-sk5.txt', lang: 'text', text: branches ?? '未提供' },
      ],
      source_ref: `${K}/project/result.md`,
      hash: refHash(K, 'project/result.md'),
      ...H,
    },
  ];

  const steps = [
    {
      id: 'pr-init', title: '① PR 发起（低风险基线）', role: 'Leader 编排', time: '17:04:12Z 建项',
      headline: 'bootstrap 基线 PR 走低风险路径——DAG 只有审查节点，无人工门',
      points: [
        { k: 'PR 内容', v: 'PR #1 bootstrap：文档与迁移基线（6 files · +168/−1）· head 4cd5bf0' },
        { k: 'run_id', v: RUN, mono: true },
        { k: 'DAG', v: '单节点 pr1sk5-review-1——审查即结论，无修复/验证/人工门' },
        { k: 'head SHA', v: SHA, mono: true },
      ],
      evidence: ['ev-auto-meta'], probe: null,
      detail: { title: '分级依据', quote: '低风险路径不是降低标准：Reviewer 仍独立审查、仍调用 SAST 与知识库——只是结论不触发停等。', outbox: [], events: [] },
    },
    {
      id: 'review', title: '② 风险审查（NOT_CONFIRMED · LOW）', role: 'Reviewer', time: '17:05:18Z → 17:06:25Z · 67 秒',
      headline: 'SAST 首次实战诚实裁决 false positive——无发现即无门，审查即结论', stat: 'NOT_CONFIRMED · LOW',
      points: [
        { k: 'Reviewer 结论', v: 'NOT_CONFIRMED · SEVERITY: LOW · HUMAN_VERIFICATION_REQUIRED: NO' },
        { k: 'SAST 实录', v: 'skill_sast_scan（inline 模式）：唯一命中 = 测试文件 subprocess.run（argv 列表、非 shell=True）——裁决 false positive；paths 模式缺部署根配置如实降级' },
        { k: '案例检索', v: 'skill_case_retrieval：无相关历史案例——如实空匹配，不以近似案例凑数' },
        { k: '时效', v: '委派 → 提交 67 秒' },
      ],
      evidence: ['ev-auto-review'], probe: null,
      exhibit: {
        type: 'terminal',
        title: 'SAST 与检索实录 · 团队房/Findings 原话',
        lines: [
          { text: 'Now run SAST scan on the changed files.', tone: 'cmd' },
          { text: 'The strict parser rejects blank context lines … use the exact file contents', tone: 'dim' },
          { text: "Only finding is the test's `subprocess.run` (not shell=True; argv list, constant args)", tone: 'ok' },
          { text: '  → 裁决：false positive（非命令注入向量）', tone: 'ok' },
          { text: 'No relevant historical match for the bootstrap PR (only PR #2/#3 cases).', tone: 'dim' },
        ],
      },
      detail: { title: '裁决纪律', quote: 'SAST 命中不等于漏洞：argv 列表 + 常量参数 ≠ 注入向量——工具命中与安全结论分层，裁决权在 Reviewer。', outbox: [], events: [] },
    },
    {
      id: 'approval', title: '③ Leader 验收 · 自动闭环', role: 'Leader', time: '17:06:38Z 自动完成',
      headline: '单节点 DAG 即审即结——低风险路径无需人工门，全程无人工介入', points: [
        { k: 'DAG 验收', v: '单节点 pr1sk5-review-1 完成 [x] · 无 ready nodes → 项目 completed' },
        { k: '终报', v: '与 PR #3 合并发送综合终报（17:06:44Z，Leader DM）' },
        { k: 'PR 状态', v: 'PR #1 保持 OPEN · 零 GitHub 写入（github-branches-after-sk5.txt 核验）' },
      ],
      evidence: ['ev-auto-run'], probe: null,
      exhibit: {
        type: 'checklist',
        title: 'Leader 验收清单 · TaskResult ×1',
        items: [
          { label: 'pr1sk5-review-1', sub: 'NOT_CONFIRMED · LOW · false positive 如实裁决 —— 验收' },
        ],
        foot: '综合终报 17:06:44Z · PR #1 保持 OPEN · 零 GitHub 写入',
      },
      decision: {
        verified: { verdict: 'NOT_EXECUTED', text: '验证未执行——审查未确认风险，无修复对象' },
        human_approval: { needed: false, text: '无需人工门——NOT_CONFIRMED/LOW 未触发停等条件（按分级设计自动完成）', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        merge_allowed: { allowed: false, text: 'PR #1 保持 OPEN —— 合并未执行' },
        github_write: { done: false, text: '未写入 GitHub', level: 'NOT_EXECUTED' },
        execution_nature: { text: 'REAL_EXECUTED-AgentTeams（低风险自动路径）：审查即结论，DAG 单节点即结', level: 'REAL_EXECUTED_AGENTTEAMS_LIVE' },
        pr_open: true,
      },
      detail: { title: '终态', quote: '低风险路径与门控路径共用同一套审查纪律——差别只在结论是否触发人工门。', outbox: [], events: [] },
    },
    {
      id: 'trace', title: '④ 追踪与观测', role: 'AgentLoop', time: '会话累计 · 824 span', stat: '会话口径',
      headline: 'SK5 会话级追踪——低风险路径同样全程留痕', points: [
        { k: '轨迹分解', v: 'span 按角色（会话累计）：leader 280 · reviewer 240 · fixer 118 · verifier 129 · reviewer-b 57——本案例为其中 reviewer 段' },
        { k: 'skill 留痕', v: 'tool.skill_sast_scan ×2 独立 span + 宿主审计流水（首战实录）' },
        { k: '用量口径', v: 'usage-summary.json 单列 pr1sk5 窗口：45 次调用 / 输入 5,084,301（99% 缓存）/ 输出 14,661' },
        { k: '披露', v: 'direct-probe 本轮修复采集（stdin 方式，4/4 非空）——此前四轮未采集的已知操作缺陷闭环' },
      ],
      evidence: ['ev-auto-run'], probe: null,
      exhibit: {
        type: 'bars',
        title: '824 span · 按角色分解（span-summary.json · 会话累计）',
        rows: [
          { label: 'leader', value: 280 },
          { label: 'reviewer', value: 240 },
          { label: 'verifier', value: 129 },
          { label: 'fixer', value: 118 },
          { label: 'reviewer-b', value: 57 },
        ],
        foot: 'skill_* ×15 · rag ×7 · delegation.link ×15 · 四包同会话',
      },
      detail: { title: '构建来源', quote: 'image/skill-mcp-server.mjs + zz_agentloop_skills.py 入包；delegation.link 数据层验证：LINK_SPAN_TRACE trace_id 与 parent_trace 一致。', outbox: [], events: [] },
    },
  ];

  return {
    case_id: 'fastapi-pr1-auto',
    portfolio_role: '低风险自动分支',
    shape: 'guided',
    name: 'FastAPI PR #1 · 低风险自动闭环',
    short_name: 'PR #1 · 低风险自动路径',
    one_liner: '低风险自动路径首次打通：bootstrap PR（6 files +168/−1）经 67 秒独立审查——NOT_CONFIRMED/LOW、SAST 唯一命中诚实裁决为 false positive——单节点 DAG 即审即结，无人工门自动完成；与门控案例共同构成风险分级处置的完整光谱。',
    repo: 'nghqqa/fastapi-boilerplate-demo',
    pr: 'PR #1 · bootstrap 基线',
    pr_url: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/1',
    run_id: RUN,
    sha: SHA,
    sha_kind: 'PR #1 head commit · ls-remote 已核验',
    evidence_level: ['REAL_EXECUTED_AGENTTEAMS_LIVE'],
    replay_note: 'REAL_EXECUTED —— 本页面回放该次运行的证据；不连接实时系统',
    purpose: '补全风险分级处置光谱：低风险自动通过——审查即结论，无需人工门',
    risk_tags: ['bootstrap 基线', 'LOW', '自动完成', '确定性 Skill MCP', 'sast_scan'],
    status: { verdict: 'PASS', label: '低风险自动完成 · 无人工门 · PR 保持 OPEN' },
    pitch_next: { to: '/evidence', kicker: '收尾', label: '原文证据索引 —— 逐字可查' },
    gate_alt: { label: 'PR #2 · 高风险门的批准路径', path: '/demo/fastapi-pr2-rag-traced-20260917' },
    stats: [
      { k: '端到端墙钟', v: '≈2 分 32 秒', hint: '建项 → 综合终报（17:04:12Z → 17:06:44Z）' },
      { k: '审查时效', v: '67 秒', hint: '委派 → Reviewer 提交' },
      { k: 'SAST', v: '×2 · 首次实战', hint: '唯一命中诚实裁决 false positive' },
      { k: 'LLM 调用', v: '45 次', hint: 'usage-summary.json pr1sk5 窗口精确值（输入 5,084,301 · 99% 缓存）' },
    ],
    facts: {
      real_github_pr: { value: '真实 GitHub PR（OPEN）', ok: true, note: 'https://github.com/nghqqa/fastapi-boilerplate-demo/pull/1' },
      real_agentteams_run: { value: '真实 AgentTeams 运行（SK5 同会话）', ok: true, note: 'NOT_CONFIRMED/LOW → 无人工门自动完成；SAST 首战如实裁决' },
      skills_mcp: { value: '确定性 Skill + SAST（advisory-only）', ok: true, note: 'sast_scan 唯一命中裁决 false positive；case_retrieval 如实空匹配' },
      github_write: { value: '未写入 —— PR 保持 OPEN', ok: false, note: 'merge/push/close/reopen 全程禁止' },
    },
    banner: null,
    stage_timeline: null,
    chain: ['案例 ' + RUN, 'PR #1', 'bootstrap 基线', 'NOT_CONFIRMED / LOW', '单节点 DAG 完成', 'project completed'],
    generated_at: '运行窗口 17:04Z–17:06Z（建项 → 自动完成；与 PR #3 同会话接力）',
    source_dir: 'evidence/FINALS-ELEM-PR1-SK5-AUTO（SHA256SUMS 锁定，39 清单项）',
    honesty: H,
    steps,
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// --------------------------------------------------- case B: rework mechanism

function caseBRework() {
  const { rework } = build();
  if (!rework.available) return null;
  const A = rework.scenarios.A_rework_then_pass;
  const finding = rework.risk_basis?.findings?.[0];
  const a1 = A.attempts?.[0];
  const a2 = A.attempts?.[1];
  const gen = rework.run_meta?.generated_at ?? null;
  const baseCode = readRepoFile('tools/agentteams/rework_case/base/payments.py');
  const baseTest = readRepoFile('tools/agentteams/rework_case/base/test_payments.py');
  const logs = {};
  for (const f of ['tests/run-rework-f1-a-attempt1.log', 'tests/run-rework-f1-a-attempt2.log']) {
    logs[f] = exists('finalsReworkLoop', f) ? readText('finalsReworkLoop', f) : null;
  }

  const items = [
    {
      id: 'ev-defect-code', title: '缺陷基线代码 rework_case/base/payments.py', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['来源', 'tools/agentteams/rework_case/base（本地案例基线，非外部 PR）'],
        ['缺陷', 'charge() 无条件 append —— 同订单重试重复扣款'],
        ['冻结验收测试', finding?.acceptance_test ?? '未提供'],
      ],
      blocks: [
        { title: 'tools/agentteams/rework_case/base/payments.py', lang: 'text', text: baseCode ?? '未提供' },
        { title: 'tools/agentteams/rework_case/base/test_payments.py（冻结，未修改）', lang: 'text', text: baseTest ?? '未提供' },
      ],
      source_ref: 'tools/agentteams/rework_case/base/（只读引用）',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-run-meta', title: '运行元数据 run-meta.json', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['run_id', 'run-rework-f1-a'], ['generated_at', gen ?? '未提供'],
        ['git_head', rework.run_meta?.git_head ?? '未提供'], ['command', rework.run_meta?.command ?? '未提供'],
        ['evidence_tier', rework.tier],
      ],
      blocks: [{ title: 'run-meta.json', lang: 'json', text: JSON.stringify(rework.run_meta, null, 2) }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/run-meta.json',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-finding', title: 'Reviewer 发现 reviewer_findings.json（受控输入）', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['发现', `${finding?.id} · ${finding?.category} · severity=${finding?.severity} · ${finding?.risk_level}`],
        ['位置', `${finding?.file}:${finding?.line}`],
        ['验收测试', finding?.acceptance_test ?? '未提供'],
        ['语义来源', 'CONTROLLED_INPUT —— 本证据等级无 LLM 调用'],
      ],
      blocks: [{ title: 'reviewer_findings.json', lang: 'json', text: JSON.stringify(rework.risk_basis, null, 2) }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/reviewer_findings.json',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-diff-a1', title: '首次修复补丁 attempt-1（受控输入）', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['文件', 'payments.py'], ['tree_sha', a1?.tree_sha ?? '未提供'],
        ['策略', '按 payment_id 去重 —— 未覆盖“同订单新 payment_id”场景'],
      ],
      blocks: [{ title: 'run-rework-f1-a-attempt1.diff', lang: 'diff', text: rework.patches?.attempt1 ?? '未提供' }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/patches/run-rework-f1-a-attempt1.diff',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-testlog-a1', title: 'Verifier 失败日志 attempt-1（FAIL）', level: 'REAL_EXECUTED',
      fields: [
        ['命令', a1?.tests?.command ?? '未提供'], ['结果', `${a1?.tests?.verdict} · ${a1?.tests?.tests_run} tests · ${a1?.tests?.failures} failure`],
        ['失败用例', (a1?.tests?.failed_tests || []).join(', ') || '—'],
        ['控制器状态', `${a1?.controller_state_after_verify?.status} / ${a1?.controller_state_after_verify?.current_stage} · verify_attempt=${a1?.controller_state_after_verify?.verify_attempt}`],
      ],
      blocks: [{ title: 'run-rework-f1-a-attempt1.log', lang: 'log', text: logs['tests/run-rework-f1-a-attempt1.log'] ?? '未提供' }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/tests/run-rework-f1-a-attempt1.log',
      real: ['unittest 真实执行，VERDICT=FAIL 由断言结果决定', '审计链记录 verify_attempt=1/3'],
      controlled: [], not_executed: ['LLM 调用', 'Matrix/Element 真实交接', 'GitHub 写入'],
    },
    {
      id: 'ev-rework-dispatch', title: 'Manager revision → Fixer 重派（dispatch outbox #4）', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['target', 'fixer · stage=fix · attempt=2'],
        ['触发', a1?.controller_state_after_verify?.last_error ?? '未提供'],
        ['rework_reason', a1?.rework_reason ?? '未提供'],
      ],
      blocks: [{ title: 'outbox body', lang: 'text', text: A.dispatch_outbox?.[3]?.body ?? '未提供' }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/report.json → scenarios.A.final.dispatch_outbox[3]',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-diff-a2', title: '二次修复补丁 attempt-2（受控输入）', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['文件', 'payments.py'], ['tree_sha', a2?.tree_sha ?? '未提供'],
        ['策略', '按 order_id 幂等 —— 重复请求返回既有扣款，不新增记录'],
      ],
      blocks: [{ title: 'run-rework-f1-a-attempt2.diff', lang: 'diff', text: rework.patches?.attempt2 ?? '未提供' }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/patches/run-rework-f1-a-attempt2.diff',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-testlog-a2', title: '第二次验证日志 attempt-2（PASS）', level: 'REAL_EXECUTED',
      fields: [
        ['命令', a2?.tests?.command ?? '未提供'], ['结果', `${a2?.tests?.verdict} · ${a2?.tests?.tests_run} tests · 0 failure`],
        ['任务终态', `${a2?.controller_state_after_verify?.status} · verdict=${a2?.controller_state_after_verify?.verdict}`],
      ],
      blocks: [{ title: 'run-rework-f1-a-attempt2.log', lang: 'log', text: logs['tests/run-rework-f1-a-attempt2.log'] ?? '未提供' }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/tests/run-rework-f1-a-attempt2.log',
      real: ['unittest 真实执行，VERDICT=PASS 由断言结果决定'],
      controlled: [], not_executed: ['LLM 调用', 'Matrix/Element 真实交接', 'GitHub 写入'],
    },
    {
      id: 'ev-stage-runs', title: '阶段序列与事件链（stage_runs · stage_events）', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['stage_runs', (A.stage_runs || []).map((s) => `${s.stage}#${s.attempt}${s.verdict ? `=${s.verdict}` : ''}`).join(' → ')],
        ['events_injected', String(rework.summary?.events_injected ?? '—')],
        ['checks', `${Object.values(A.checks || {}).filter(Boolean).length}/${Object.keys(A.checks || {}).length} 通过`],
      ],
      blocks: [{ title: 'stage_events（event_id ↔ stage）', lang: 'json', text: JSON.stringify(A.stage_events, null, 2) }],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/report.json → scenarios.A.final',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-final-event', title: '验证通过终态事件（verifier TASK_COMPLETED #2）', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['event_id', A.stage_events?.[5]?.event_id ?? '未提供'],
        ['sender', A.stage_events?.[5]?.sender ?? '未提供'],
        ['含义', 'verify attempt=2 完成 → 控制器判定任务 PASS（终态，无后续派单）'],
      ],
      blocks: [],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/report.json → scenarios.A.final.stage_events[5]',
      ...REWORK_HONESTY,
    },
    {
      id: 'ev-controller-final', title: '控制器配置与结局签名', level: 'CONTROL_PLANE_MECHANISM',
      fields: [
        ['controller', rework.controller?.file ?? '未提供'], ['mode', rework.controller?.mode ?? '未提供'],
        ['MAX_VERIFY_ATTEMPTS', String(rework.controller?.max_verify_attempts ?? '—')],
        ['outcome_signature', rework.outcome_signature ?? '未提供'],
      ],
      blocks: [],
      source_ref: 'evidence/FINALS-REWORK-LOOP-20260914/report.json',
      ...REWORK_HONESTY,
    },
  ];

  const steps = [
    {
      id: 'defect', title: '缺陷与验收条件', role: 'Reviewer（受控输入）', time: '基线固化',
      headline: '冻结的验收测试先行——缺陷、预期行为、风险分级全部前置声明',
      points: [
        { k: '缺陷', v: 'payments.py:29 charge() 无条件 append —— 网关重试携带新 payment_id → 同一订单被重复扣款' },
        { k: '冻结验收测试', v: finding?.acceptance_test ?? '未提供', mono: true },
        { k: '预期行为', v: finding?.expected_behavior ?? '未提供' },
        { k: '风险分级', v: 'L1 · correctness · 无密钥/依赖/破坏性操作，允许自动修复' },
        { k: '语义来源', v: '受控输入（CONTROLLED_INPUT）—— 本证据等级无 LLM、无真实 AgentTeams' },
      ],
      evidence: ['ev-defect-code', 'ev-finding'],
      detail: { title: '派单记录', outbox: A.dispatch_outbox?.slice(0, 2) ?? [], events: A.stage_events?.slice(0, 2) ?? [] },
    },
    {
      id: 'attempt1', title: '首次修复结果', role: 'Fixer', time: 'attempt=1',
      headline: 'Fixer 第一次修复：按 payment_id 去重——看似合理，未覆盖同订单新 id 场景',
      points: [
        { k: '补丁 attempt-1', v: '按 payment_id 去重 —— 看似合理，未覆盖同订单新 payment_id 场景' },
        { k: 'tree_sha', v: a1?.tree_sha ?? '未提供', mono: true },
        { k: '交付', v: 'fixer attempt=1 COMPLETED（控制器 stage_runs 记录）' },
      ],
      evidence: ['ev-diff-a1'],
      detail: { title: '执行记录', outbox: A.dispatch_outbox?.slice(2, 3) ?? [], events: A.stage_events?.slice(2, 3) ?? [] },
    },
    {
      id: 'verify-fail', title: 'Verifier 失败', role: 'Verifier', time: 'verify_attempt=1/3', tone: 'reject',
      headline: '独立复测不采信 Fixer 自述——冻结验收测试真实执行，FAIL 1/5 触发返工',
      points: [
        { k: '验证命令', v: a1?.tests?.command ?? '未提供', mono: true },
        { k: '结果', v: 'FAIL · 5 tests · 1 failure —— total_for_order(1)=1000 ≠ 500（AssertionError）' },
        { k: '失败用例', v: 'test_second_payment_request_for_same_order_is_idempotent', mono: true },
        { k: '控制器状态', v: `${a1?.controller_state_after_verify?.status} / ${a1?.controller_state_after_verify?.current_stage} · verify_attempt=${a1?.controller_state_after_verify?.verify_attempt}/3` },
        { k: '判定来源', v: '冻结验收测试真实执行结果（REAL_EXECUTED）—— 由测试决定，非预置' },
      ],
      evidence: ['ev-testlog-a1'],
      exhibit: {
        type: 'terminal',
        title: 'Verifier 失败日志 · run-rework-f1-a-attempt1.log（摘要）',
        lines: [
          { text: '$ ' + (a1?.tests?.command ?? 'python -m unittest'), tone: 'cmd' },
          { text: 'FAIL: test_second_payment_request_for_same_order_is_idempotent', tone: 'bad' },
          { text: 'AssertionError: total_for_order(1) → got 1000, want 500', tone: 'bad' },
          { text: `VERDICT: FAIL · ${a1?.tests?.tests_run ?? 5} tests · ${a1?.tests?.failures ?? 1} failure → verify_attempt=${a1?.controller_state_after_verify?.verify_attempt ?? 1}/3 触发返工`, tone: 'amber' },
        ],
      },
      detail: { title: '失败事件', outbox: [], events: A.stage_events?.slice(3, 4) ?? [] },
    },
    {
      id: 'revision', title: 'Manager revision → Fixer 重派', role: 'Workflow Controller', time: 'attempt=2 重派',
      headline: '判定 revision 并回退修复——失败原因随重派一并下发，attempt 计数受控上限 3',
      points: [
        { k: 'revision 判定', v: a1?.rework_reason ?? 'verify FAIL（第 1/3 次），回退修复', mono: false },
        { k: '重派', v: 'dispatch outbox → fixer · stage=fix · attempt=2' },
        { k: '二次补丁', v: 'attempt-2 改为按 order_id 幂等（重复请求返回既有扣款）' },
        { k: 'tree_sha', v: a2?.tree_sha ?? '未提供', mono: true },
      ],
      evidence: ['ev-rework-dispatch', 'ev-diff-a2'],
      detail: { title: '重派与重修记录', outbox: A.dispatch_outbox?.slice(3, 4) ?? [], events: A.stage_events?.slice(4, 5) ?? [] },
    },
    {
      id: 'verify-pass', title: '第二次验证通过', role: 'Verifier', time: 'verify_attempt=2/3',
      headline: '同一套冻结验收测试第二次执行——5/5 全部通过，返工闭环完成',
      points: [
        { k: '结果', v: 'PASS · 5/5 全部通过（同一套冻结验收测试）' },
        { k: '任务终态', v: `PASS · verifier TASK_COMPLETED ${A.stage_events?.[5]?.event_id ?? ''}（PostgreSQL 审计链）`, mono: false },
        { k: '外部 PR', v: '不适用 —— 控制面机制案例，不作为外部 PR 证据' },
        { k: 'GitHub 写入', v: '未执行 —— NOT_EXECUTED' },
      ],
      evidence: ['ev-testlog-a2', 'ev-final-event', 'ev-stage-runs'],
      detail: { title: '终态事件链', outbox: [], events: A.stage_events?.slice(5) ?? [] },
    },
  ];

  return {
    case_id: 'rework-payments',
    portfolio_role: '案例 C · 返工机制（控制面）',
    shape: 'guided',
    name: '支付幂等 · 返工机制案例',
    short_name: '机制案例 · 支付幂等',
    one_liner: 'Verifier 只认冻结的验收测试：首次修复测试失败，Manager 判定 revision 重派 Fixer，第二次修复通过。',
    repo: 'mergepilot/payments-demo（本地案例仓库）',
    pr: 'PR #7（本地编号，非可核验外部 PR）',
    pr_url: null,
    run_id: 'run-rework-f1-a',
    sha: a2?.tree_sha ?? null,
    sha_kind: 'attempt-2 tree_sha',
    evidence_level: ['CONTROL_PLANE_MECHANISM'],
    replay_note: null,
    purpose: '展示 verify-FAIL → revision → 重派 → 复验的返工语义（控制面机制）',
    risk_tags: ['业务逻辑幂等', 'correctness', 'L1'],
    status: { verdict: 'PASS', label: 'attempt-2 验证通过 5/5 · 机制闭环' },
    facts: {
      real_github_pr: { value: '否 —— 控制面机制案例，不作为外部 PR 证据', ok: false, note: 'mergepilot/payments-demo PR#7 无可核验外部链接' },
      real_agentteams_run: { value: '否 —— 无 LLM、无 Matrix/Element 交接', ok: false, note: 'Reviewer/Fixer 语义为受控输入' },
      github_write: { value: '未写入 —— NOT_EXECUTED', ok: false, note: '' },
    },
    banner: '这是控制面机制案例，不是外部 GitHub PR；测试和状态链路用于展示返工语义。',
    stage_timeline: (A.stage_runs || []).map((s) => ({
      label: `${{ review: 'Review', fix: 'Fix', verify: 'Verify' }[s.stage] || s.stage} #${s.attempt}`,
      verdict: s.verdict,
    })),
    chain: ['案例 rework-payments', 'PR #7（本地）', `tree ${short(a2?.tree_sha, 10)}`, 'payments.py 补丁', 'unittest 报告', `任务 ${A.final_task?.status ?? 'PASS'}`],
    generated_at: gen,
    source_dir: 'evidence/FINALS-REWORK-LOOP-20260914 · tools/agentteams/rework_case/base',
    honesty: REWORK_HONESTY,
    steps,
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// ------------------------------------------- case C (additional): DB SQL loop

function caseCDbLoop() {
  const { db } = build();
  if (!db.available) return null;
  const r = exists('finalsDbLoop', 'report.json') ? readJson('finalsDbLoop', 'report.json') : null;
  const rev1 = db.attempts.find((a) => a.revision === 1);
  const rev2 = db.attempts.find((a) => a.revision === 2 && !a.late_callback);
  const rev1Detail = (r.steps || []).find((s) => s.step === 'verify_rev1_attempt1')?.detail ?? null;
  const rev2Detail = (r.steps || []).find((s) => s.step === 'verify_rev2_attempt1')?.detail ?? null;
  const mig = (revFile) => (exists('finalsDbLoop', `case/migrations/candidate-a.${revFile}.sql`) ? readText('finalsDbLoop', `case/migrations/candidate-a.${revFile}.sql`) : null);
  const gen = db.run_meta?.generated_at ?? null;

  const items = [
    {
      id: 'ev-baseline', title: '历史基线（S1_baseline）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['baseline_id', db.baseline?.baseline_id ?? '未提供'],
        ['行数', Object.entries(db.baseline?.row_counts || {}).map(([k, v]) => `${k}=${v}`).join(' · ') || '未提供'],
        ['历史 NULL customer_id', String(db.baseline?.historical_null_customer_id ?? '—')],
        ['重复支付行', String(db.baseline?.historical_duplicate_payment_rows ?? '—')],
        ['PostgreSQL', db.environment?.pg_version ?? '未提供'],
      ],
      blocks: [{ title: 'baseline/schema.sql', lang: 'sql', text: exists('finalsDbLoop', 'case/baseline/schema.sql') ? readText('finalsDbLoop', 'case/baseline/schema.sql') : '未提供' }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/report.json → S1_baseline',
      ...DB_HONESTY,
    },
    {
      id: 'ev-mig-rev1', title: '迁移候选 rev1（直接加约束）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['candidate_id', 'mc-31b545c0e9f6c391a44e19f5'],
        ['head_sha', rev1?.head_sha ?? '未提供'], ['script_digest', rev1?.script_digest ?? '未提供'],
      ],
      blocks: [{ title: 'candidate-a.rev1.sql', lang: 'sql', text: mig('rev1') ?? '未提供' }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/case/migrations/candidate-a.rev1.sql',
      ...DB_HONESTY,
    },
    {
      id: 'ev-fail-23502', title: 'rev1 验证失败（SQLSTATE 23502）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['verification_id', rev1?.verification_id ?? '未提供'],
        ['failure_class', 'HISTORICAL_DATA_INCOMPATIBLE'],
        ['migration error', rev1?.migration?.error?.message ?? '未提供'],
        ['断言', `${rev1?.assertions?.passed}/${rev1?.assertions?.total} 通过`],
      ],
      blocks: [{ title: '失败断言明细', lang: 'json', text: JSON.stringify(rev1Detail?.assertions ?? rev1?.assertions ?? {}, null, 2) }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/report.json → verify_rev1_attempt1',
      ...DB_HONESTY,
    },
    {
      id: 'ev-context-fetch', title: '上下文补取与修订决策（S5b）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['missing_context', db.context_fetch?.missing_context ?? '未提供'],
        ['可回填 / 哨兵', `${db.context_fetch?.fetched?.legacy_order_owner_coverage ?? '—'} / ${db.context_fetch?.fetched?.unresolvable_orders ?? '—'}`],
        ['unresolvable_share', String(db.context_fetch?.unresolvable_share ?? '—')],
        ['decision', `${db.context_fetch?.decision ?? '—'}（≤ 20% → 修订候选，否则升级人工）`],
      ],
      blocks: [{ title: '补取查询', lang: 'sql', text: db.context_fetch?.fetched?.query ?? '未提供' }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/report.json → S5b_context_fetch',
      ...DB_HONESTY,
    },
    {
      id: 'ev-mig-rev2', title: '迁移候选 rev2（回填+归档+兼容）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['candidate_id', 'mc-109d93c429f38e6f5139654e'],
        ['parent_candidate', 'mc-31b545c0e9f6c391a44e19f5'],
        ['head_sha', rev2?.head_sha ?? '未提供'], ['script_digest', rev2?.script_digest ?? '未提供'],
      ],
      blocks: [{ title: 'candidate-a.rev2.sql', lang: 'sql', text: mig('rev2') ?? '未提供' }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/case/migrations/candidate-a.rev2.sql',
      ...DB_HONESTY,
    },
    {
      id: 'ev-assert-pass', title: 'rev2 验证通过（11/11 断言）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['verification_id', rev2?.verification_id ?? '未提供'],
        ['结果', `PASS · ${rev2?.assertions?.passed}/${rev2?.assertions?.total} 断言通过`],
        ['clone → migrate', `${rev2Detail?.clone_ms ?? '—'} ms → ${rev2Detail?.migration?.duration_ms ?? '—'} ms`],
        ['代码测试', `PASS · 7 tests（${rev2?.code_tests?.command ?? ''}）`],
      ],
      blocks: [{ title: '断言明细', lang: 'json', text: JSON.stringify(rev2Detail?.assertions ?? {}, null, 2) }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/report.json → verify_rev2_attempt1',
      ...DB_HONESTY,
    },
    {
      id: 'ev-approval-bind', title: '真实审批记录（绑定 verification + head）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['ticket_id', db.approval?.ticket_id ?? '未提供'],
        ['bound_verification', db.approval?.bound_verification_id ?? '未提供'],
        ['bound_head', db.approval?.bound_head_sha ?? '未提供'],
        ['approved', `${db.approval?.approved === true ? 'APPROVED' : '未提供'} · by ${db.approval?.approved_by ?? '—'}`],
      ],
      blocks: [],
      note: '审批在真实审计库中绑定（verification_id, head_sha）；本演示界面不重复该操作，只展示记录。',
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/report.json → S7_*',
      ...DB_HONESTY,
    },
    {
      id: 'ev-gate-stale', title: 'follow-up 提交后审批失效（gate timeline）', level: 'LOCAL_REAL_SQL',
      fields: [
        ['followup head', db.followup?.head_sha ?? '未提供'],
        ['gate_after', db.followup?.gate_after ?? '未提供'],
        ['followup 变更', db.followup?.change ?? '未提供'],
      ],
      blocks: [{ title: 'gate_timeline', lang: 'json', text: JSON.stringify(db.gate_timeline ?? [], null, 2) }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/report.json → gate_timeline',
      ...DB_HONESTY,
    },
    {
      id: 'ev-plan-package', title: '迁移计划包与负向测试', level: 'LOCAL_REAL_SQL',
      fields: [
        ['plan path', db.plan_package?.path ?? '未提供'],
        ['plan files', (db.plan_package?.files || []).join(' · ') || '未提供'],
        ['negative tests', `${db.negative_tests?.ok ?? '—'}/${db.negative_tests?.total ?? '—'} 通过`],
        ['final_disposition', '见下方 blocks'],
      ],
      blocks: [{ title: 'final_disposition', lang: 'json', text: JSON.stringify(db.final_disposition ?? {}, null, 2) }],
      source_ref: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914/report.json',
      ...DB_HONESTY,
    },
  ];

  return {
    case_id: 'db-migration-orders',
    portfolio_role: '附录 · 数据库迁移与版本绑定',
    shape: 'brief',
    extra: true,
    name: '订单库迁移 · 历史数据兼容',
    short_name: '附加证据 · 数据库迁移',
    one_liner: '迁移在隔离 PostgreSQL 真实执行：rev1 被历史数据挡下（23502），上下文补取后 rev2 11/11 断言通过。',
    repo: 'mergepilot/orders-demo',
    pr: 'PR #4 · orders-schema-change',
    pr_url: null,
    run_id: 'run-dbv-084723-rev2',
    sha: rev2?.head_sha ?? null,
    sha_kind: 'rev2 head（git tree object id）',
    evidence_level: ['LOCAL_REAL_SQL'],
    replay_note: null,
    purpose: '附加证据：数据库迁移与历史数据兼容风险的本地真实 SQL 验证',
    risk_tags: ['数据库迁移', '历史数据兼容', 'SQLSTATE 23502'],
    status: { verdict: 'PASS', label: '验证通过 11/11 · 审批后被 follow-up 取代（STALE）' },
    facts: {
      real_github_pr: { value: '否 —— 本地案例（SYNTHETIC 数据）', ok: false, note: '' },
      real_agentteams_run: { value: '否 —— 真实 SQL 执行，但非 AgentTeams 运行', ok: false, note: '' },
      github_write: { value: '未写入 —— NOT_EXECUTED', ok: false, note: '' },
    },
    banner: null,
    stage_timeline: null,
    chain: null,
    generated_at: gen,
    source_dir: 'evidence/FINALS-DB-MIGRATION-LOOP-20260914',
    honesty: DB_HONESTY,
    blocks: [
      {
        id: 'risk', title: '风险类型',
        rows: [
          { k: '风险', v: '数据库迁移 · 历史数据兼容 —— 新代码假设的约束直接加在含脏数据的存量库上' },
          { k: '触发点', v: 'orders.customer_id SET NOT NULL + payments.order_id UNIQUE；存量库含 137 条 NULL customer_id、2 条重复支付' },
          { k: '数据模式', v: 'SYNTHETIC（orders-demo 合成基线 10000 订单 / 10002 支付）· PolarDB NOT CONNECTED' },
        ],
        evidence: ['ev-baseline', 'ev-mig-rev1'],
      },
      {
        id: 'evidence', title: '关键证据',
        rows: [
          { k: 'rev1 验证', v: 'FAIL / HISTORICAL_DATA_INCOMPATIBLE —— SQLSTATE 23502：orders.customer_id 含 NULL；断言 3/11' },
          { k: '上下文补取', v: 'legacy_order_owner 覆盖 120 条，17 条不可解（12.41% ≤ 20%）→ REVISE_CANDIDATE 而非升级人工' },
          { k: 'rev2 修订', v: '回填 120 + 哨兵 17（写入审计表）+ 归档重复支付 + DEFAULT 0 保旧程序兼容 + 最后加约束' },
        ],
        evidence: ['ev-fail-23502', 'ev-context-fetch', 'ev-mig-rev2'],
      },
      {
        id: 'verdict', title: '验证结论',
        rows: [
          { k: 'rev2 验证', v: `PASS · 11/11 断言 · verification_id=${rev2?.verification_id ?? '—'}` },
          { k: '执行环境', v: '隔离 PostgreSQL 16.14 clone（CREATE DATABASE … TEMPLATE）· 真实 SQL 执行' },
        ],
        evidence: ['ev-assert-pass'],
      },
      {
        id: 'disposition', title: '最终处置',
        rows: [
          { k: '人工审批', v: `已执行并记录于真实审计库：ticket ${db.approval?.ticket_id ?? '—'} 绑定 verification+head，approved_by=${db.approval?.approved_by ?? '—'}` },
          { k: 'gate 语义', v: `follow-up 提交（${db.followup?.change ?? ''}）后审批 STALE_SUPERSEDED_BY_NEW_REVISION → 发布阻断，需对新 head 重新验证` },
          { k: '生产发布', v: 'NOT PERFORMED —— trial clone 不是生产发布；执行需按计划包受控发布' },
          { k: 'GitHub 写入', v: '未执行 —— NOT_EXECUTED' },
        ],
        evidence: ['ev-approval-bind', 'ev-gate-stale', 'ev-plan-package'],
      },
    ],
    decision: {
      verified: { verdict: 'PASS', text: '11/11 断言（隔离 PostgreSQL 真实执行）' },
      human_approval: { needed: true, text: '需要且已记录（S7_l2_approve · 绑定 verification_id + head_sha）', level: 'LOCAL_REAL_SQL' },
      merge_allowed: { allowed: false, text: '数据库发布门：仅当审批绑定当前 head 时放行；follow-up 后 STALE，需重新验证' },
      github_write: { done: false, text: '未写入 —— NOT_EXECUTED', level: 'NOT_EXECUTED' },
      execution_nature: { text: '历史回放 · LOCAL_REAL_SQL：隔离 PostgreSQL 真实执行；案例数据 SYNTHETIC', level: 'LOCAL_REAL_SQL' },
    },
    evidence_index: items.map(({ id, title, level }) => ({ id, title, level })),
    items,
  };
}

// ------------------------------------------------------------------ public API

const CASE_BUILDERS = { 'fastapi-pr2-rag-traced-20260917': caseGPr2Sk5Traced, 'fastapi-pr1-auto': caseIPr1Sk5Auto, 'fastapi-pr2-cwe22': caseAFastAPI, 'fastapi-pr2-live-20260916': caseDPr2Live, 'fastapi-pr3-reject': caseEPr3Reject, 'rag-retrieval-loop': caseFRagLoop, 'rework-payments': caseBRework, 'db-migration-orders': caseCDbLoop };

function summarize(x) {
  if (!x) return null;
  // Overview/queue surfaces are pre-decision by definition (gateChoice is
  // per-page-load, never persisted) — gated cases must ship pending-safe
  // fields so entry pages don't leak the recorded outcome.
  const hasGate = (x.steps ?? []).some((s) => s.id === 'gate');
  return {
    case_id: x.case_id, shape: x.shape, extra: !!x.extra, name: x.name, short_name: x.short_name,
    has_gate: hasGate,
    name_pending: hasGate ? (x.name_pending ?? x.name) : null,
    pr_pending: hasGate ? String(x.pr ?? '').split(' · ')[0] : null,
    one_liner: x.one_liner, repo: x.repo, pr: x.pr, pr_url: x.pr_url ?? null, run_id: x.run_id,
    portfolio_role: x.portfolio_role ?? null, relation_note: x.relation_note ?? null,
    sha: x.sha, sha_kind: x.sha_kind, evidence_level: x.evidence_level, replay_note: x.replay_note ?? null,
    purpose: x.purpose, risk_tags: x.risk_tags, status: x.status, facts: x.facts, stats: x.stats ?? null,
    generated_at: x.generated_at, source_dir: x.source_dir, steps: x.steps?.length ?? null,
  };
}

export function demoOverview() {
  const b = build();
  const a = caseAFastAPI();
  const live = caseGPr2Sk5Traced() || caseDPr2Live();
  const r3 = caseEPr3Reject();
  const p1 = caseIPr1Sk5Auto();
  const c = caseBRework();
  const d = caseCDbLoop();
  const f = caseFRagLoop();
  // 2026-09-19 lineup: ONLY the two SK5-TRACED core cases are demoed (PR #2 approve / PR #3 reject);
  // the prior V3-TRACED round stays reachable via the legacy /cases/pr* archive replay.
  // Mechanism cases (rework loop / DB migration / RAG observe→evaluate→tune→backtest)
  // answer the judges' specific asks and are listed as additional evidence entries;
  // every other builder remains reachable by direct id (deep links / API) but is
  // intentionally not part of the roadshow lineup.
  return {
    available: !!(live && r3),
    platform: {
      name: 'MergePilot',
      tagline: '证据驱动的 PR 审修流程回放器 · 离线优先',
      data_note: '本平台回放锁定证据目录中的运行证据；演示页面不连接实时模型 / GitHub / Matrix / PolarDB',
      integrity: Object.entries(b.integrity).map(([k, v]) => ({ key: k, dir: v.dir, tier: v.tier, verified: !!v.verified, files: v.files ?? 0 })),
    },
    levels: EVIDENCE_LEVELS,
    current_case_id: live ? live.case_id : (r3 ? r3.case_id : null),
    cases: [p1, live, r3].filter(Boolean).map(summarize).filter((x) => !x.extra),
    additional: [c, d, f].filter(Boolean).map(summarize),
  };
}

export function demoCases() {
  const o = demoOverview();
  return { available: o.available, levels: o.levels, cases: o.cases, additional: o.additional };
}

export function demoCase(id) {
  const buildCase = CASE_BUILDERS[id];
  if (!buildCase) return null;
  const c = buildCase();
  return c ?? { available: false, reason: `${id} 所需证据目录不可用` };
}

export function demoEvidence(caseId, evidenceId) {
  const c = demoCase(caseId);
  if (!c || !c.items) return null;
  const item = c.items.find((x) => x.id === evidenceId);
  if (!item) return null;
  const { items, steps, blocks, decision, ...meta } = c;
  return {
    ...item,
    case: { case_id: meta.case_id, case_name: meta.name, repo: meta.repo, pr: meta.pr, pr_url: meta.pr_url ?? null, run_id: meta.run_id, sha: meta.sha, sha_kind: meta.sha_kind, evidence_level: meta.evidence_level, source_dir: meta.source_dir, generated_at: meta.generated_at, pending_redactions: meta.pending_redactions ?? null },
    integrity: { finals: build().integrity },
  };
}
