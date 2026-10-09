// status-vocab.smoke.test.mjs — PR-2 审批错误态区分 + 状态词表单源化 冒烟：
//  A) status-map.js 词表完备性（纯函数直测）：
//     TICKET 补 STALE/CONSUMED；SEVERITY 补 P0-P3；STAGE 补 PENDING 且入 STAGE_ORDER；
//     新增 MU_RUN（14 态，与后端 orchestration.mjs RUN_STATES 权威对齐）与
//     MU_ATTEMPT（5 态含 TIMEOUT）；unknownEntry fail-closed 不吞未知值。
//     rc.10 PR-C 补：LEADER_DECISION / PROTECTION / MU_FIX / MU_VERIFY / WIRE（A2 区）。
//  B) 渲染冒烟（esbuild 真打包 + react-test-renderer + happy-dom）：
//     MuApprovals 四态分离（空态 / 网络失败+重试 / 401 / 403）与状态列中文；
//     CorePage WAITING_FOR_HUMAN_APPROVAL 走共享表；PendingPage P0-P3 自然生效；
//     PipelinePanel attempt TIMEOUT → 已超时；theme.js STATUS_META 收敛到 status-map 口径。
import test from 'node:test';
import assert from 'node:assert/strict';
import { after } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';
import { MemoryRouter } from 'react-router-dom';
import { Window } from 'happy-dom';

// 后端权威状态机（console/backend/lib/multiuser/orchestration.mjs，仅依赖 node:crypto，可直连）——
// 前端 MU_RUN 词表必须覆盖全部 14 态，键漂移在此第一时间报警。
import { RUN_STATES } from '../../backend/lib/multiuser/orchestration.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(__dirname, '..');
const toFwd = (p) => p.replace(/\\/g, '/');

const hasCJK = (s) => /[\u4e00-\u9fff]/.test(String(s ?? ''));

// ── A. 词表完备性（纯函数，无 DOM）──
test('词表：TICKET 全 7 键（含 STALE/CONSUMED）都有中文 label+note', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  const keys = ['PENDING', 'APPROVED', 'REJECTED', 'USED', 'EXPIRED', 'STALE', 'CONSUMED'];
  for (const k of keys) {
    const e = m.TICKET[k];
    assert.ok(e, `TICKET.${k} 存在`);
    assert.ok(hasCJK(e.label), `TICKET.${k}.label 中文（=${e.label}）`);
    assert.ok(hasCJK(e.note) && e.note.includes(k), `TICKET.${k}.note 中文且含原始枚举`);
    assert.ok(['ok', 'info', 'warn', 'bad', 'neutral'].includes(e.tone), `TICKET.${k}.tone 合法`);
  }
  assert.equal(m.TICKET.STALE.label, '已失效（PR head 已更新）');
  assert.equal(m.TICKET.CONSUMED.label, '已批准并放行修复轮');
  assert.equal(m.ticketMap('STALE').label, m.TICKET.STALE.label, 'ticketMap(STALE) 走词表');
});

test('词表：SEVERITY 补 P0-P3（危急/高/中/低，tone 与 CRITICAL-LOW 对齐）并入 ORDER', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  const expect = { P0: ['危急', 'bad'], P1: ['高', 'bad'], P2: ['中', 'warn'], P3: ['低', 'warn'] };
  for (const [k, [label, tone]] of Object.entries(expect)) {
    const e = m.SEVERITY[k];
    assert.ok(e, `SEVERITY.${k} 存在`);
    assert.equal(e.label, label, `SEVERITY.${k}.label=${label}`);
    assert.equal(e.tone, tone, `SEVERITY.${k}.tone=${tone}（与 CRITICAL-LOW 档位对齐）`);
    assert.ok(hasCJK(e.note) && e.note.includes(k), `SEVERITY.${k}.note 中文且含原始枚举`);
  }
  for (const k of ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']) {
    assert.ok(m.SEVERITY_ORDER.includes(k), `原 4 键保留：${k}`);
  }
  assert.deepEqual(m.SEVERITY_ORDER.slice(4), ['P0', 'P1', 'P2', 'P3'], 'P0-P3 进入筛选/排序词表');
  // PendingPage 消费契约：筛选选项与排序秩都从 SEVERITY/SEVERITY_ORDER 派生——P0-P3 自然生效
  assert.ok(m.SEVERITY_ORDER.length >= 8);
});

test('词表：STAGE.PENDING=待审查（PR 尚无审查运行）且 STAGE_ORDER 含 PENDING（overview 桶可见）', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  assert.ok(m.STAGE.PENDING, 'STAGE.PENDING 存在');
  assert.equal(m.STAGE.PENDING.label, '待审查');
  assert.ok(hasCJK(m.STAGE.PENDING.note) && m.STAGE.PENDING.note.includes('PENDING'));
  assert.ok(m.STAGE_ORDER.includes('PENDING'), 'PENDING 在 STAGE_ORDER（图表桶不再隐身）');
  assert.equal(m.STAGE_ORDER[0], 'PENDING', 'PENDING 为最前（初始阶段）');
  assert.equal(m.stageMap('pending').label, '待审查', 'stageMap 大小写归一');
});

test('词表：MU_RUN 覆盖后端 RUN_STATES 全部 14 态（权威对齐，含 WAITING_FOR_HUMAN_APPROVAL）', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  assert.equal(RUN_STATES.length, 14, `后端 RUN_STATES 14 态（实际 ${RUN_STATES.length}）`);
  for (const s of RUN_STATES) {
    const e = m.MU_RUN[s];
    assert.ok(e, `MU_RUN.${s} 存在（后端权威态不得缺键）`);
    assert.ok(hasCJK(e.label), `MU_RUN.${s}.label 中文（=${e.label}）`);
    assert.ok(hasCJK(e.note) && e.note.includes(s), `MU_RUN.${s}.note 中文且含原始枚举`);
  }
  assert.equal(Object.keys(m.MU_RUN).length, RUN_STATES.length, 'MU_RUN 无多余键（与后端同边界）');
  assert.equal(m.MU_RUN.WAITING_FOR_HUMAN_APPROVAL.label, '待人工批准（高危修复）');
  assert.equal(m.muRunMap('waiting_for_human_approval').label, '待人工批准（高危修复）');
});

test('词表：MU_ATTEMPT 全 5 态含 TIMEOUT=已超时（agent_attempt CHECK 值域）', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  const keys = ['RUNNING', 'DONE', 'FAILED', 'TIMEOUT', 'SKIPPED'];
  for (const k of keys) {
    const e = m.MU_ATTEMPT[k];
    assert.ok(e, `MU_ATTEMPT.${k} 存在`);
    assert.ok(hasCJK(e.label) && hasCJK(e.note), `MU_ATTEMPT.${k} 中文 label+note`);
  }
  assert.equal(m.MU_ATTEMPT.TIMEOUT.label, '已超时');
  assert.equal(m.MU_ATTEMPT.TIMEOUT.tone, 'bad', '超时非结论性失败但显式标红档');
  assert.equal(m.muAttemptMap('TIMEOUT').label, '已超时');
});

test('词表：unknownEntry fail-closed——未知枚举保留原始值并标注未知状态，不吞键', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  for (const map of [m.ticketMap, m.muRunMap, m.muAttemptMap, m.stageMap]) {
    const e = map('MYSTERY_V9');
    assert.equal(e.label, 'MYSTERY_V9', `${map.name}: 原始机器值保留在 label`);
    assert.ok(e.note.includes('未知状态') && e.note.includes('MYSTERY_V9'), `${map.name}: note 标注未知状态`);
  }
});

// ── A2. rc.10 PR-C 新增词表（纯函数直测；值域对齐后端权威定义）──
test('词表：LEADER_DECISION 覆盖 leader.mjs decideAfterReview 4 值域（needs_human→需人工裁定）', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  const keys = ['clean_complete', 'fix_required', 'needs_human', 'blocked'];
  for (const k of keys) {
    const e = m.LEADER_DECISION[k];
    assert.ok(e, `LEADER_DECISION.${k} 存在`);
    assert.ok(hasCJK(e.label) && e.label.length <= 12, `LEADER_DECISION.${k}.label 中文（=${e.label}）`);
    assert.ok(hasCJK(e.note) && e.note.includes(k), `LEADER_DECISION.${k}.note 中文且含原始枚举`);
    assert.ok(['ok', 'info', 'warn', 'bad', 'neutral'].includes(e.tone), `LEADER_DECISION.${k}.tone 合法`);
  }
  assert.equal(m.leaderDecisionMap('needs_human').label, '需人工裁定');
  assert.equal(m.leaderDecisionMap('needs_human').tone, 'warn', '等待人工=warn（非失败）');
  assert.equal(m.leaderDecisionMap('blocked').tone, 'bad', '受控停止=bad 档');
  assert.equal(m.leaderDecisionMap('MYSTERY').label, 'MYSTERY', '未知 decision fail-closed 不吞键');
});

test('词表：PROTECTION 覆盖 api.mjs 白名单 3 值域（unknown=未知时不判定可合并）', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  const keys = ['known_clean', 'blocked', 'unknown'];
  for (const k of keys) {
    const e = m.PROTECTION[k];
    assert.ok(e, `PROTECTION.${k} 存在`);
    assert.ok(hasCJK(e.label) && hasCJK(e.note) && e.note.includes(k), `PROTECTION.${k} 中文 label+note 含原始枚举`);
  }
  assert.equal(m.protectionMap('known_clean').label, '受保护');
  assert.equal(m.protectionMap('known_clean').tone, 'ok');
  const u = m.protectionMap('unknown');
  assert.equal(u.label, '保护状态未知');
  assert.ok(u.note.includes('不判定可合并'), 'unknown note 写明 fail-closed 语义');
  assert.equal(m.protectionMap(null).label, '保护状态未知', '缺值按 unknown 归一（不冒充受保护）');
});

test('词表：MU_FIX 覆盖 fix_attempt CHECK 4 值域；MU_VERIFY 覆盖 verification_attempt 4 值域', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  for (const k of ['PLANNED', 'DRY_RUN', 'FAILED', 'SKIPPED']) {
    const e = m.MU_FIX[k];
    assert.ok(e, `MU_FIX.${k} 存在`);
    assert.ok(hasCJK(e.label) && hasCJK(e.note) && e.note.includes(k), `MU_FIX.${k} 中文 label+note 含原始枚举`);
  }
  assert.equal(m.muFixMap('DRY_RUN').label, '隔离预演');
  assert.equal(m.muFixMap('dry_run').label, '隔离预演', '大小写归一');
  for (const k of ['PASS', 'FAIL', 'BLOCKED', 'INCONCLUSIVE']) {
    const e = m.MU_VERIFY[k];
    assert.ok(e, `MU_VERIFY.${k} 存在`);
    assert.ok(hasCJK(e.label) && hasCJK(e.note) && e.note.includes(k), `MU_VERIFY.${k} 中文 label+note 含原始枚举`);
  }
  assert.equal(m.muVerifyMap('PASS').tone, 'ok');
  assert.equal(m.muVerifyMap('INCONCLUSIVE').tone, 'warn', '不确定=warn（非失败）');
});

test('词表：WIRE 接线状态 6 键中文 label+note（已接入/联调已接入/未接线/未交付/关闭/不适用）', async () => {
  const m = await import(pathToFileURL(path.join(FRONTEND, 'src/status-map.js')).href);
  const keys = ['wired', 'wired_test', 'not_wired', 'pending_delivery', 'closed', 'na'];
  for (const k of keys) {
    const e = m.WIRE[k];
    assert.ok(e, `WIRE.${k} 存在`);
    assert.ok(hasCJK(e.label) && hasCJK(e.note), `WIRE.${k} 中文 label+note`);
    assert.ok(['ok', 'info', 'warn', 'bad', 'neutral'].includes(e.tone), `WIRE.${k}.tone 合法`);
  }
  assert.equal(m.wireMap('wired').label, '已接入');
  assert.equal(m.wireMap('closed').tone, 'neutral', '关闭=中性事实');
  assert.equal(m.wireMap('MYSTERY').label, 'MYSTERY', '未知 wire 值 fail-closed 不吞键');
});

// ── B. 渲染冒烟基建（复用 pages-smoke 模式：esbuild 真打包 + react-test-renderer + happy-dom）──
const domWindow = new Window();
if (!globalThis.window) globalThis.window = domWindow;
for (const k of ['HTMLElement', 'SVGElement', 'ShadowRoot', 'Element', 'Node', 'Document',
  'MouseEvent', 'KeyboardEvent', 'Event', 'CustomEvent', 'DOMRect', 'ResizeObserver', 'MutationObserver']) {
  if (domWindow[k] && !globalThis[k]) globalThis[k] = domWindow[k];
}
globalThis.document = domWindow.document;
globalThis.navigator ??= domWindow.navigator;
globalThis.getComputedStyle ??= domWindow.getComputedStyle.bind(domWindow);
globalThis.matchMedia ??= domWindow.matchMedia.bind(domWindow);
globalThis.requestAnimationFrame ??= domWindow.requestAnimationFrame.bind(domWindow);
globalThis.cancelAnimationFrame ??= domWindow.cancelAnimationFrame.bind(domWindow);

const SESSION = { user: { name: 'smoke' }, repos: [] };
let ROUTES = {};
let calls = [];
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  calls.push(`${input?.method ?? 'GET'} ${u.pathname}${u.search}`);
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const out = route(u);
  const [status, body] = (out && typeof out.then === 'function') ? await out : out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

let cachedMeta = null;
// 每个数据源模式一个独立 bundle 实例：bundle 的 ESM 模块缓存跨测试共享，
// data/config.js 的模块级 config 缓存会把首个 health 响应泄露给后续用例——
// snapshot 组与 multiuser 组必须物理隔离（等价于分文件的既有测试惯例）。
async function loadAppBundle(group = 'mu') {
  const outDir = path.join(FRONTEND, 'node_modules', `.status-vocab-smoke-${group}`);
  const bundle = path.join(outDir, 'bundle.cjs');
  if (!fs.existsSync(bundle)) {
    const entry = path.join(outDir, 'entry.mjs');
    fs.mkdirSync(outDir, { recursive: true });
    fs.writeFileSync(entry, [
      `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};`,
      `import { STATUS_META } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/theme.js')))};`,
      'export { App, STATUS_META };',
    ].join('\n'));
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
  }
  const mod = await import(pathToFileURL(bundle).href);
  return mod;
}

async function renderRoute(route, group = 'mu') {
  const mod = await loadAppBundle(group);
  cachedMeta = mod.STATUS_META;
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(mod.App)),
    );
  });
  for (let i = 0; i < 24; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}

function walk(node, fn) {
  if (node == null) return;
  fn(node);
  if (typeof node !== 'object') return;
  (node.children ?? []).forEach((c) => walk(c, fn));
}
// 可见文本：仅拼接字符串子节点——title 等属性不进可见文本（raw enum 允许留在 title/技术详情）
function allText(node) {
  let t = '';
  walk(node, (nd) => { if (typeof nd === 'string') t += nd; });
  return t;
}
function findButtonByText(node, text) {
  let hit = null;
  walk(node, (nd) => {
    if (hit || nd.type !== 'button') return;
    let t = '';
    walk(nd, (x) => { if (typeof x === 'string') t += x; });
    if (t.includes(text)) hit = nd;
  });
  return hit;
}

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

// ── 通用路由夹具 ──
const MU_HEALTH = { '/api/health': () => [200, {
  service: 'console', data_mode: 'live',
  sources: { primary: 'multiuser', multiuser: { available: true } },
}] };
const MU_SESSION = { '/api/mu/session': () => [200, {
  user: { user_id: 'u-1', login: 'm1' },
  tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
  role: 'maintainer', actions: ['read_pull_request', 'decide_review'], memberships: [],
}] };
const SNAPSHOT_HEALTH = { '/api/health': () => [200, { service: 'console', data_mode: 'snapshot' }] };
const LEGACY_SESSION = { '/api/auth/session': () => [200, SESSION] };

const APPROVAL_ROWS = [
  { approval_id: 'ap-1', run_id: 'r-1', finding_id: 'f-1', severity: 'P0', status: 'STALE',
    head_sha: 'aa'.repeat(20), pr_number: 4242, repo_owner: 'acme', repo_name: 'app',
    rule_id: 'R-SECRET', path: 'a.js', line_start: 3, summary_masked: 'sk-***',
    created_at: '2026-10-02T10:00:00Z', expires_at: '2026-10-09T00:00:00Z',
    decided_by: null, decided_at: null, run_status: 'WAITING_FOR_HUMAN_APPROVAL' },
  { approval_id: 'ap-2', run_id: 'r-1', finding_id: 'f-2', severity: 'P1', status: 'CONSUMED',
    head_sha: 'bb'.repeat(20), pr_number: 4242, repo_owner: 'acme', repo_name: 'app',
    rule_id: 'R-SQL', path: 'b.js', line_start: 5, summary_masked: 'q***',
    created_at: '2026-10-02T10:05:00Z', expires_at: '2026-10-09T00:00:00Z',
    decided_by: 'mu:m1', decided_at: '2026-10-02T14:00:00Z', run_status: 'FIX_QUEUED' },
];

function muApprovalsRoute(handler) {
  return { '/api/mu/approvals': handler };
}

test('MuApprovals 空态（200+[]）：诚实零值文案，无重试钮、无错误块', async () => {
  ROUTES = { ...LEGACY_SESSION, ...MU_HEALTH, ...MU_SESSION, ...muApprovalsRoute(() => [200, { approvals: [] }]) };
  calls = [];
  const { renderer } = await renderRoute('/approvals');
  try {
    const visible = allText(renderer.toJSON());
    assert.ok(visible.includes('当前没有审批票'), '空态诚实零值上屏');
    assert.ok(!visible.includes('审批票读取失败'), '200 不得渲染网络错误块');
    assert.ok(!visible.includes('登录已过期'), '200 不得渲染 401 文案');
    assert.equal(findButtonByText(renderer.toJSON(), '重试'), null, '空态无重试钮');
    // 筛选下拉中文（值仍为 enum，在 DOM 属性中）
    assert.ok(visible.includes('全部状态') && visible.includes('已失效（PR head 已更新）'),
      '筛选下拉“全部状态”+中文状态项');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('MuApprovals 网络失败：显式错误块+重试钮，绝不渲染成“没有审批票”；点击重试重新请求', async () => {
  let fail = true;
  ROUTES = { ...LEGACY_SESSION, ...MU_HEALTH, ...MU_SESSION, ...muApprovalsRoute(() => {
    if (fail) throw new Error('ECONNREFUSED');
    return [200, { approvals: [] }];
  }) };
  calls = [];
  const { renderer } = await renderRoute('/approvals');
  try {
    const before = calls.filter((c) => c.startsWith('GET /api/mu/approvals')).length;
    let visible = allText(renderer.toJSON());
    assert.ok(visible.includes('审批票读取失败（网络或服务不可用）'), '网络失败显式错误块上屏');
    assert.ok(!visible.includes('诚实零值'), '网络失败绝不冒充诚实零值（空态标记文案不出现）');
    const retry = findButtonByText(renderer.toJSON(), '重试');
    assert.ok(retry, '重试钮存在');
    fail = false; // 重试后恢复服务——验证错误态可恢复到空态
    await act(async () => { retry.props.onClick({ preventDefault() {}, stopPropagation() {} }); });
    for (let i = 0; i < 10; i++) await act(async () => { await Promise.resolve(); });
    const after = calls.filter((c) => c.startsWith('GET /api/mu/approvals')).length;
    assert.equal(after, before + 1, '重试重新触发既有 load（请求参数契约不变）');
    // 重试成功后错误块消失、空态出现（同一错误块容器，不残留旧文案）
    visible = allText(renderer.toJSON());
    assert.ok(!visible.includes('审批票读取失败') && visible.includes('诚实零值'),
      '重试成功回到诚实零值（错误态可恢复）');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('MuApprovals 401/403 分离：过期引导重登 vs 角色无权限，两者都不是空态', async () => {
  // 401
  ROUTES = { ...LEGACY_SESSION, ...MU_HEALTH, ...MU_SESSION, ...muApprovalsRoute(() => [401, { error: { reason: 'unauthorized' } }]) };
  let r = await renderRoute('/approvals');
  try {
    const visible = allText(r.renderer.toJSON());
    assert.ok(visible.includes('登录已过期——请刷新页面重新登录'), '401 过期文案上屏');
    assert.ok(!visible.includes('当前没有审批票') && !visible.includes('无审批票读取权限'), '401 与 403/空态不混淆');
  } finally { await act(async () => { r.renderer.unmount(); }); }
  // 403
  ROUTES = { ...LEGACY_SESSION, ...MU_HEALTH, ...MU_SESSION, ...muApprovalsRoute(() => [403, { error: { reason: 'forbidden' } }]) };
  r = await renderRoute('/approvals');
  try {
    const visible = allText(r.renderer.toJSON());
    assert.ok(visible.includes('当前角色无审批票读取权限（403 如实）'), '403 保留既有无权限文案');
    assert.ok(!visible.includes('登录已过期') && !visible.includes('当前没有审批票'), '403 与 401/空态不混淆');
  } finally { await act(async () => { r.renderer.unmount(); }); }
});

test('MuApprovals 状态列/级别列中文：STALE/CONSUMED/P0 行走词表，raw enum 只在 title', async () => {
  ROUTES = { ...LEGACY_SESSION, ...MU_HEALTH, ...MU_SESSION, ...muApprovalsRoute(() => [200, { approvals: APPROVAL_ROWS }]) };
  const { renderer, json } = await renderRoute('/approvals');
  try {
    const visible = allText(renderer.toJSON());
    assert.ok(visible.includes('已失效（PR head 已更新）'), 'STALE→已失效（PR head 已更新）');
    assert.ok(visible.includes('已批准并放行修复轮'), 'CONSUMED→已批准并放行修复轮');
    assert.ok(visible.includes('危急') && visible.includes('高'), 'P0/P1→危急/高');
    assert.ok(!/\bSTALE\b/.test(visible) && !/\bCONSUMED\b/.test(visible) && !/\bPENDING\b/.test(visible),
      '可见文本无裸 STALE/CONSUMED/PENDING（P0/P1 属页脚量纲词汇，不在禁止列）');
    const text = json();
    assert.ok(text.includes('approval.tickets: STALE') && text.includes('approval.tickets: CONSUMED'),
      'raw enum 保留在 title 技术详情');
    assert.ok(text.includes('"title"'), 'title 属性存在（机器值收纳处）');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('theme.js STATUS_META 收敛到 status-map 口径（票据/严重度键单源派生）', async () => {
  ROUTES = { ...LEGACY_SESSION, ...MU_HEALTH, ...MU_SESSION, ...muApprovalsRoute(() => [200, { approvals: [] }]) };
  const { renderer } = await renderRoute('/approvals');
  try {
    const meta = cachedMeta;
    assert.ok(meta, 'STATUS_META 可从 bundle 导入');
    assert.equal(meta.PENDING.label, '待审批', 'PENDING 收敛到 TICKET 口径（原「等待处理」）');
    assert.equal(meta.APPROVED.label, '已批准');
    assert.equal(meta.HIGH.label, '高', 'HIGH 收敛到 SEVERITY 口径（原「高风险」）');
    assert.equal(meta.P0.label, '危急', 'P0 补齐进 STATUS_META');
    assert.equal(meta.STALE.label, '已失效（PR head 已更新）', 'STALE 补齐进 STATUS_META');
    assert.equal(meta.POSTGRESQL_LIVE.label, '实时数据', '数据源键不受影响');
    assert.equal(meta.MU_CANONICAL_LIVE.label, 'MU 实时数据', 'MU 数据源键不受影响');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('CorePage：WAITING_FOR_HUMAN_APPROVAL / FIX_QUEUED 行走共享 MU_RUN 表，无裸枚举', async () => {
  ROUTES = {
    ...LEGACY_SESSION, ...SNAPSHOT_HEALTH,
    '/api/pulls': () => [200, { source: 'MU_CANONICAL_LIVE', data_source: 'MU_CANONICAL_LIVE', pulls: [] }],
    '/api/pending': () => [200, { source: 'MU_CANONICAL_LIVE', pending: [
      { run_id: 'run-wait', status: 'WAITING_FOR_HUMAN_APPROVAL', repo: 'acme/app', pr: 9, updated_at: '2026-10-02T00:00:00Z' },
      { run_id: 'run-fix', status: 'FIX_QUEUED', repo: 'acme/app', pr: 10, updated_at: '2026-10-02T01:00:00Z' },
    ] }],
    '/api/tickets': () => [200, { tickets: [] }],
    '/api/evidence': () => [200, { evidence: [] }],
    '/api/audit': () => [200, { source: 'MU_CANONICAL_LIVE', audit: [] }],
    '/api/fxv/attempts': () => [200, { attempts: [] }],
    '/api/fxv/metrics': () => [200, {}],
  };
  const { renderer } = await renderRoute('/core', 'snapshot');
  try {
    const visible = allText(renderer.toJSON());
    assert.ok(visible.includes('待人工批准（高危修复）'), 'WAITING_FOR_HUMAN_APPROVAL→待人工批准（高危修复）');
    assert.ok(visible.includes('修复排队'), 'FIX_QUEUED→修复排队（共享表）');
    assert.ok(!/\bWAITING_FOR_HUMAN_APPROVAL\b/.test(visible) && !/\bFIX_QUEUED\b/.test(visible),
      '可见文本无裸 run 枚举（raw 在 Tag title）');
    const text = JSON.stringify(renderer.toJSON());
    assert.ok(text.includes('WAITING_FOR_HUMAN_APPROVAL'), 'raw enum 保留在 title 技术详情');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('PendingPage：P0-P3 严重度列与筛选经扩展词表自然生效，可见文本无裸枚举', async () => {
  ROUTES = {
    ...LEGACY_SESSION, ...SNAPSHOT_HEALTH,
    '/api/runs': () => [200, { items: [
      { repo: 'acme/app', pr_number: 7, created_at: '2026-10-02T10:00:00Z',
        review: { verdict: 'FINDING_CONFIRMED', severity: 'P0' }, execution: { status: 'COMPLETED' } },
      { repo: 'acme/app', pr_number: 8, created_at: '2026-10-02T11:00:00Z',
        review: { verdict: 'FINDING_CONFIRMED', severity: 'P3' }, execution: { status: 'COMPLETED' } },
    ] }],
  };
  const { renderer } = await renderRoute('/pending', 'snapshot');
  try {
    const visible = allText(renderer.toJSON());
    assert.ok(visible.includes('危急') && visible.includes('低'), 'P0→危急、P3→低（列与筛选选项同源）');
    assert.ok(visible.includes('全部严重度'), '严重度筛选上屏');
    assert.ok(!/\bPENDING\b/.test(visible) && !/\bSTALE\b/.test(visible) && !/\bCONSUMED\b/.test(visible)
      && !/\bTIMEOUT\b/.test(visible) && !/\bWAITING_FOR_HUMAN_APPROVAL\b/.test(visible),
      'PendingPage 可见文本无裸枚举');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('PipelinePanel：attempt TIMEOUT → 已超时（共享 MU_ATTEMPT 表），run 态走 MU_RUN', async () => {
  ROUTES = {
    ...LEGACY_SESSION, ...MU_HEALTH, ...MU_SESSION,
    '/api/mu/auth/providers': () => [200, { github: { configured: true } }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: '9001', binding_id: 'b-1', installation_state: 'active' },
    ] }],
    '/api/mu/github/app/status': () => [200, { configured: true, app_id: 999, permissions: ['contents:read'] }],
    '/api/mu/github/installations': () => [200, { installations: [] }],
    '/api/mu/prs': () => [200, { pull_requests: [
      { pr_id: 'p-2', provider_pr_number: 102, head_sha: 'bbb2', branch_protection_status: 'known_clean', updated_at: '2026-09-28T11:00:00Z' },
    ] }],
    '/api/mu/prs/102': () => [200, {
      pull_request: { pr_id: 'p-2', provider_pr_number: 102, repo_owner: 'acme', repo_name: 'app', head_sha: 'bbb2', branch_protection_status: 'known_clean' },
      review_records: [],
    }],
    '/api/mu/prs/p-2/fix-approvals': () => [200, { fix_approvals: [] }],
    '/api/mu/runs': () => [200, { runs: [{ run_id: 'run-9', provider_pr_number: 102, status: 'WAITING_FOR_HUMAN_APPROVAL', trigger_source: 'webhook', head_sha: 'bbb2' }] }],
    '/api/mu/runs/run-9': () => [200, {
      run: { run_id: 'run-9', status: 'WAITING_FOR_HUMAN_APPROVAL', trigger_source: 'webhook', head_sha: 'bbb2' },
      findings: [],
      attempts: [
        { agent_role: 'reviewer', attempt: 1, status: 'TIMEOUT', provider: 'agentteams', latency_ms: 99999, created_at: '2026-09-29T11:00:00Z', error_code: 'AGENT_TIMEOUT' },
        { agent_role: 'reviewer', attempt: 2, status: 'DONE', provider: 'agentteams', latency_ms: 1200, created_at: '2026-09-29T11:05:00Z' },
      ],
      fixes: [], verifications: [], dead_letters: [],
    }],
  };
  const { renderer } = await renderRoute('/mu/repos/acme/app/pr/102');
  try {
    const visible = allText(renderer.toJSON());
    assert.ok(visible.includes('已超时'), 'TIMEOUT→已超时（新增键全覆盖）');
    assert.ok(visible.includes('待人工批准（高危修复）'), 'run 态走 MU_RUN 共享表');
    assert.ok(!/\bTIMEOUT\b/.test(visible), '可见文本无裸 TIMEOUT（raw 在 title/error_code）');
  } finally { await act(async () => { renderer.unmount(); }); }
});
