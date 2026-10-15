// ux-fixpack.smoke.test.mjs — 前端交付可信度最小修复包（2026-10-15）冒烟：
// 1) UX-05 合并 result_uncertain 恢复入口：409+operation_id 后「读回对账」与文案同屏，
//    对账只调 /reconcile 且绑定正确 operation，不再发第二次 merge-pr/confirm；
//    对账定案（merged/failed）后入口撤下；未定案保留可重试。
// 2) UX-06 审批票摘要全文可达：真实 button 展开/收起，aria-expanded/aria-controls 关联。
// 3) UX-16 ApprovalsPage 分流：mu / console-pg 两分支均可渲染（Hooks 数量恒定的结构
//    保证 + 双分支冒烟；配置异步翻转场景无法在本环境真实构造，不做虚假断言）。
// 复用 mu-detail-actions 基建：esbuild 打包真实 App + fetch 路由 mock + happy-dom 真点击。
import test from 'node:test';
import assert from 'node:assert/strict';
import { after } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { Window } from 'happy-dom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(__dirname, '..');
const toFwd = (p) => p.replace(/\\/g, '/');

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
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const SESSION = { user: { name: 'smoke' }, repos: [] };
const HEAD = 'ab'.repeat(20);
const MERGE_OP_ID = '6f1e2c3a-1111-2222-3333-444455556666';
let ROUTES = {};
let postCounts = {};
globalThis.__smokeHits = {};
globalThis.__smokeErrs = [];
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const method = String(init?.method ?? 'GET').toUpperCase();
  const route = ROUTES[u.pathname];
  if (method === 'POST') postCounts[u.pathname] = (postCounts[u.pathname] ?? 0) + 1;
  if (!route) {
    return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  }
  const out = route(u, init);
  if (out instanceof Promise) return out; // 延迟受控响应（防双击/进行中断言用）
  const [status, body] = out;
  globalThis.__smokeHits = globalThis.__smokeHits ?? {};
  globalThis.__smokeHits[`${method} ${u.pathname}`] = (globalThis.__smokeHits[`${method} ${u.pathname}`] ?? 0) + 1;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.ux-fixpack-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};
import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};\nexport { App, clearRuntimeConfigCache };`);
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  const pick = (k) => mod[k] ?? mod.default?.[k] ?? null;
  return { App: pick('App'), clear: pick('clearRuntimeConfigCache') };
}

const MU_HEALTH = () => [200, {
  service: 'mergepilot-console', data_mode: 'live',
  sources: { primary: 'multiuser', multiuser: { available: true, reason: 'mu_mode_active' } },
}];
const PG_HEALTH = () => [200, {
  service: 'console', data_mode: 'fixture',
  sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } },
}];

const DETAIL = (actions) => [200, {
  pull_request: { pr_id: 'pr-1', provider_pr_number: 4242, head_sha: HEAD,
    branch_protection_status: 'known_clean', title: 'fix' },
  review_records: [],
  latest_run: { run_id: 'run-1', status: 'REVIEWED', architecture_version: 'v2',
    review_mode: 'evidence_only' },
  my_permissions: { actions },
}];

const LONG_SUMMARY = 'applyDiscount 未对 cart 为 null/undefined 做防护，直接访问 cart.items 会抛出 TypeError。'
  + '新增测试 tests/discount.test.js 明确断言 applyDiscount(null) 抛出 TypeError，'
  + '并覆盖 items 为空数组与非法入参两条路径——修复应保持既有调用方兼容。';

function baseRoutes(actions) {
  return {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': MU_HEALTH,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'smoke1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions, memberships: [],
    }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001' }] }],
    '/api/mu/prs/4242': () => DETAIL(actions),
    '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: [
      { approval_id: 'ap-1', severity: 'P1', rule_id: 'LLM-logic_bug', path: 'discount.js',
        line_start: 6, summary_masked: LONG_SUMMARY, head_sha: HEAD, status: 'PENDING',
        decided_by: null, decided_at: null, expires_at: '2026-10-20T00:00:00Z' }] }],
    '/api/mu/runs': () => [200, { runs: [
      { run_id: 'run-1', provider_pr_number: 4242, head_sha: HEAD, status: 'REVIEWED',
        findings_count: 1, top_severity: 'P1', trigger_source: 'webhook',
        created_at: '2026-10-15T07:39:00Z', updated_at: '2026-10-15T07:40:00Z' }] }],
    '/api/mu/runs/run-1/artifacts': () => [200, {
      run: { run_id: 'run-1', owner: 'acme', repo_name: 'app', provider: 'github',
        head_sha: HEAD, created_at: '2026-10-15T07:39:00Z' },
      findings: [], outputs: {}, parse_state: {},
      patch: { patch_id: 'p-1', patch_digest: 'd1'.repeat(4), head_sha: HEAD, files: [],
        applicability: { ok: true, git_apply: { ok: true, verified: true } },
        test_verification: { verified: false } },
      publish: null }],
    '/api/mu/approvals': () => [200, { approvals: [] }],
    '/api/mu/runs/run-1/merge-pr/preview': () => [200, {
      ok: true, can_merge: true,
      target: { provider: 'github', repo: 'acme/app', pr_number: 4242,
        head: HEAD, source_branch: 'feature', target_branch: 'main' },
      merge_conditions: { pr_open: true, mergeable: true, review: 'pass', ci: { state: 'pass' } },
      robot_identity: 'mergepilot-bot', operation_note: 'n', blocked: [],
      applied_commit: null }],
  };
}

async function renderRoute(route) {
  const loaded = await loadApp();
  if (loaded.clear) loaded.clear();
  const container = domWindow.document.createElement('div');
  domWindow.document.body.appendChild(container);
  let root = null;
  await act(async () => {
    root = createRoot(container);
    root.render(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(loaded.App)));
  });
  for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); });
  return {
    text: () => container.textContent ?? '',
    clickButton: (label) => {
      const btn = findButtons(domWindow.document, label)[0];
      if (!btn) {
        assert.ok(false, `可点击目标：${label}——当前页面文本：${(container.textContent ?? '').slice(0, 400)}`);
      }
      btn.click();
    },
    cleanup: async () => {
      if (root) await act(async () => root.unmount());
      container.remove();
    },
  };
}

function findButtons(scope, label) {
  const needle = label.replace(/\s+/g, '');
  return [...scope.querySelectorAll('button')]
    .filter((b) => (b.textContent ?? '').replace(/\s+/g, '').includes(needle));
}
// 读回对账按钮：必须存在于 Drawer 之外（结果不明时 Drawer 已关闭——同屏可达的语义）
function findReconButtonOutsideDrawer() {
  return [...domWindow.document.querySelectorAll('button')]
    .filter((b) => (b.textContent ?? '').replace(/s+/g, '').includes('读回对账'))
    .find((b) => !b.closest('.ant-drawer'));
}
const bodyText = (needle) => (domWindow.document.body.textContent ?? '').includes(needle);
const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

let appMod = null;

// ── 1. UX-05：合并 result_uncertain → 对账入口同屏、绑定正确 operation、不发第二次 confirm ──
test('合并 result_uncertain：读回对账与文案同屏；对账绑定 merge operation；不重发 merge-confirm', async () => {
  postCounts = {};
  appMod ??= await loadApp();
  ROUTES = {
    ...baseRoutes(['read_pull_request', 'request_review', 'decide_review', 'request_repair', 'apply_fix', 'merge_pr']),
    '/api/mu/runs/run-1/merge-pr/confirm': () => [409, {
      error: { reason: 'result_uncertain', operation_id: MERGE_OP_ID, blocked: null } }],
    [`/api/mu/apply-operations/${MERGE_OP_ID}/reconcile`]: () => [200, {
      ok: true, reconciled: true, status: 'merged',
      merge_sha: 'ff'.repeat(20), note: '读回确认：PR 已合并（成功，仅响应丢失）' }],
  };
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    await act(async () => { page.clickButton('预览并合并 PR'); });
    await flush();
    assert.ok(domWindow.document.body.textContent.includes('合并 PR 预览'), '合并预览 Drawer 打开（portal 到 body）');
    await act(async () => { page.clickButton('确认合并 PR'); });
    await flush();
    assert.equal(postCounts['/api/mu/runs/run-1/merge-pr/confirm'], 1, 'confirm 仅一次');
    assert.ok(bodyText('合并结果不明'), '结果不明文案上屏（Drawer 外同屏）');
    // 关键：恢复入口在 Drawer 之外（与文案同屏可达），且绑定同一 operation
    const reconBtn = findReconButtonOutsideDrawer();
    assert.ok(reconBtn, '「读回对账」按钮在 Drawer 外可达（与文案同屏）');
    await act(async () => { reconBtn.click(); });
    await flush();
    assert.equal(postCounts[`/api/mu/apply-operations/${MERGE_OP_ID}/reconcile`], 1,
      '对账 POST 绑定 merge operation 的 operation_id');
    assert.equal(postCounts['/api/mu/runs/run-1/merge-pr/confirm'], 1,
      '对账路径绝不重新调用 merge-confirm');
    assert.ok(bodyText('读回确认：PR 已合并'), '对账定案文案上屏（后端 note）');
    assert.equal(findReconButtonOutsideDrawer(), undefined,
      '定案后 Drawer 外「结果不明」入口撤下');
    assert.ok(!bodyText('合并结果不明'), '结果不明 Alert 撤下');
  } finally { await page.cleanup(); }
});

// ── 2. UX-05 补充：对账仍不确定 → 入口保留可重试；HTTP 失败不误报已定案 ──
test('对账仍不确定：入口保留；对账失败：文案如实且不撤入口', async () => {
  postCounts = {};
  appMod ??= await loadApp();
  ROUTES = {
    ...baseRoutes(['read_pull_request', 'merge_pr']),
    '/api/mu/runs/run-1/merge-pr/confirm': () => [409, {
      error: { reason: 'result_uncertain', operation_id: MERGE_OP_ID } }],
    [`/api/mu/apply-operations/${MERGE_OP_ID}/reconcile`]: () => [200, {
      ok: true, reconciled: false, status: 'result_uncertain',
      note: '远端不可达——保持 uncertain，稍后重试对账' }],
  };
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    await act(async () => { page.clickButton('预览并合并 PR'); });
    await flush();
    await act(async () => { page.clickButton('确认合并 PR'); });
    await flush();
    await act(async () => { findReconButtonOutsideDrawer().click(); });
    await flush();
    assert.ok(bodyText('保持 uncertain'), '未定案文案如实');
    assert.equal(postCounts[`/api/mu/apply-operations/${MERGE_OP_ID}/reconcile`], 1);
    assert.ok(findReconButtonOutsideDrawer(), '未定案 → Drawer 外入口保留可重试');
    // 第二次对账（HTTP 500）——不得误报成功/定案
    ROUTES[`/api/mu/apply-operations/${MERGE_OP_ID}/reconcile`] = () => [500,
      { error: { reason: 'reconcile_internal_error' } }];
    await act(async () => { findButtons(domWindow.document, '读回对账')[0].click(); });
    await flush();
    assert.ok(bodyText('对账未完成'), '对账失败文案如实');
    assert.ok(findReconButtonOutsideDrawer(), '失败后 Drawer 外入口仍在');
  } finally { await page.cleanup(); }
});

// ── 3. UX-06：审批票摘要展开（真实 button + aria 关联）──
test('审批票长摘要：默认截断，「展开全文/收起」切换 aria-expanded，全文在 DOM 可达', async () => {
  postCounts = {};
  appMod ??= await loadApp();
  ROUTES = baseRoutes(['read_pull_request', 'decide_review']);
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    assert.ok(page.text().includes('高危修复审批'),
      `审批区上屏——fetch 命中：${JSON.stringify(globalThis.__smokeHits ?? {})}——页面：${page.text().slice(0, 260)}`);
    const expand = findButtons(domWindow.document, '展开全文')[0];
    assert.ok(expand, '「展开全文」按钮存在');
    assert.equal(expand.tagName, 'BUTTON', '真实 button——键盘可达（Tab+Enter 原生语义）');
    assert.equal(expand.getAttribute('aria-expanded'), 'false');
    const controls = expand.getAttribute('aria-controls');
    assert.ok(controls, 'aria-controls 关联摘要元素');
    assert.ok(domWindow.document.getElementById(controls), '关联的摘要元素存在');
    assert.ok(page.text().includes('修复应保持既有调用方兼容'), '长摘要全文在 DOM（截断仅视觉）');
    await act(async () => { expand.click(); });
    await flush();
    const collapse = findButtons(domWindow.document, '收起')[0];
    assert.ok(collapse, '展开后按钮变「收起」');
    assert.equal(collapse.getAttribute('aria-expanded'), 'true', 'aria-expanded 翻转');
  } finally { await page.cleanup(); }
});

// ── 4. UX-16：ApprovalsPage mu / console-pg 双分支渲染（分流后 Hooks 数量恒定）──
test('审批页分流：multiuser 分支与 console-pg 分支均正常渲染', async () => {
  appMod ??= await loadApp();
  ROUTES = {
    ...baseRoutes(['read_pull_request']),
    '/api/mu/approvals': () => [200, { approvals: [] }],
  };
  const pageMu = await renderRoute('/approvals', appMod);
  try {
    assert.ok(pageMu.text().includes('待审批'), 'MU 分支标题上屏');
    assert.ok(pageMu.text().includes('当前没有审批票'), 'MU 分支真空态上屏');
  } finally { await pageMu.cleanup(); }
  // 切 console-pg 源（独立挂载实例——结构上每分支 Hooks 恒定）
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': PG_HEALTH,
    '/pg/api/approvals': () => [200, { items: [] }],
  };
  const pagePg = await renderRoute('/approvals', appMod);
  try {
    assert.ok(pagePg.text().includes('待审批'), 'pg 分支标题上屏');
    assert.ok(pagePg.text().includes('合成票据演练'), 'pg 分支控件上屏');
  } finally { await pagePg.cleanup(); }
});
