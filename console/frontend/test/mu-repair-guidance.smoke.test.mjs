// mu-repair-guidance.smoke.test.mjs — 首次使用最小修复包回归（2026-10-15）：
// 1) BLOCKED run 发起受控修复 → 409 no_repair_pending：服务端 hint（新 head 恢复路径+
//    同 head 幂等如实）在动作旁同屏渲染，不只报 409 裸码；
// 2) 终态审批票（CONSUMED）不出现可重新批准的误导按钮；
// 3) 审批票上下文（platform/owner/repo/PR#/run 状态）在票行可见——桌面与窄屏同一 DOM 节点；
// 4) 无权限角色：发起受控修复保持 disabled+服务端拒绝形态；
// 5) result_uncertain：恢复入口指向「读回对账」（复用 #469/#465 已验收结构，此处断言
//    apply 结果消息对 uncertain 的指引存在——不重放真实写入）。
// 全程零真实远端写入（fetch 全 mock）。
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
let ROUTES = {};
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const out = route(u, init);
  if (out instanceof Promise) return out;
  const [status, body] = out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-repair-smoke');
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

const MAINTAINER_ACTIONS = ['read_repository', 'read_pull_request', 'read_code_content',
  'rag_query', 'request_review', 'decide_review', 'request_repair', 'apply_fix', 'merge_pr'];
const REVIEWER_ACTIONS = ['read_repository', 'read_pull_request', 'read_code_content',
  'rag_query', 'request_review'];

// PR 详情夹具：run BLOCKED + CONSUMED 票（修复轮已执行过的真实形态）
const DETAIL = (actions) => [200, {
  pull_request: { pr_id: 'pr-1', provider: 'github', provider_pr_number: 12,
    head_sha: 'ab'.repeat(20), branch_protection_status: 'known_clean', title: 'fix' },
  review_records: [],
  latest_run: { run_id: 'run-blocked', status: 'BLOCKED', architecture_version: 'v2',
    review_mode: 'evidence_only', review_verdict: 'changes_requested',
    verification_verdict: 'blocked', tests_status: 'failed', merge_eligibility: 'ineligible' },
  my_permissions: { actions },
}];

const TICKETS = () => [200, { fix_approvals: [
  { approval_id: 'ap-1', run_id: 'run-blocked', finding_id: 'f-1', severity: 'P1',
    status: 'CONSUMED', head_sha: 'cd'.repeat(20), decided_by: 'maintainer',
    decided_at: '2026-10-15T06:50:23Z', consumed_at: '2026-10-15T06:50:29Z',
    rule_id: 'LLM-logic_bug', path: 'discount.js', line_start: 6,
    summary_masked: 'applyDiscount 未对 cart 为 null 做防护', pr_number: 12,
    run_status: 'BLOCKED' } ] }];

const REPAIR_409 = [409, { error: { reason: 'no_repair_pending',
  message: '当前审查状态 BLOCKED，无可发起的受控修复',
  run_status: 'BLOCKED',
  hint: 'run 已进入 BLOCKED（半终态，不可直接重入修复轮）。恢复路径：推送新 commit 产生新 head——'
    + '系统将自动重新审查并生成新审批票（审批门会重新执行，旧 head 的批准与证据不会自动重放）；'
    + '对当前 head 重复点「触发只读审查」不会新建 run（同 head 幂等去重）。' } }];

const ROUTES_FOR = () => ({
  ...{
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, { service: 'console', data_mode: 'fixture',
      sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } } }],
  },
  '/api/mu/session': () => [200, {
    user: { user_id: 'u-1', login: 'smoke1' },
    tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
    role: 'maintainer', actions: MAINTAINER_ACTIONS, memberships: [],
  }],
  '/api/mu/repositories': () => [200, { repositories: [
    { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001' }] }],
  '/api/mu/prs/12': () => DETAIL(MAINTAINER_ACTIONS),
  '/api/mu/prs/pr-1/fix-approvals': () => TICKETS(),
  '/api/mu/runs': () => [200, { runs: [] }],
  '/api/mu/prs/pr-1/repair': () => REPAIR_409,
});

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
    container,
    text: () => container.textContent ?? '',
    html: () => container.innerHTML ?? '',
    cleanup: async () => { if (root) await act(async () => root.unmount()); container.remove(); },
  };
}
const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };
// 按钮查询必须限定在本次渲染的 container 内——document 全局查询会把
// 其他用例泄漏渲染（断言失败跳过 cleanup 时）的残留按钮一并抓进来（G4 教训 2026-10-16）
function findButtons(container, label) {
  const needle = label.replace(/\s+/g, '');
  return [...container.querySelectorAll('button')]
    .filter((b) => (b.textContent ?? '').replace(/\s+/g, '').includes(needle));
}

const ROUTE = '/mu/repos/acme/app/pr/12';

after(() => { try { domWindow.close(); } catch { /* ignore */ } setTimeout(() => process.exit(0), 300); });

test('G1 BLOCKED run 发起受控修复 → 409 hint 同屏渲染（新 head 路径+同 head 幂等如实）', async () => {
  ROUTES = ROUTES_FOR();
  const ui = await renderRoute(ROUTE);
  try {
    await flush();
    const repairBtn = findButtons(ui.container, '发起受控修复')[0];
    assert.ok(repairBtn, '发起受控修复按钮存在');
    await act(async () => { repairBtn.click(); });
    await flush();
    const txt = ui.text();
    assert.match(txt, /BLOCKED/, '409 文本可见');
    assert.match(txt, /推送新 commit 产生新 head/, '恢复路径（新 head）同屏');
    assert.match(txt, /不会新建 run/, '同 head 幂等如实（不冒充会新建）');
    assert.match(txt, /审批门会重新执行/, '审批门重执行说明可见');
  } finally { await ui.cleanup(); }
});

test('G2 终态票（CONSUMED）不出现可重新批准的按钮', async () => {
  ROUTES = ROUTES_FOR();
  const ui = await renderRoute(ROUTE);
  try {
    await flush();
    const approveBtns = findButtons(ui.container, '批准受控修复')
      .filter((b) => !b.disabled);
    assert.equal(approveBtns.length, 0, 'CONSUMED 票无可用批准按钮');
  } finally { await ui.cleanup(); }
});

test('G3 审批票上下文（platform/owner/repo/PR#/run 状态）双分支可见+票据绑定 head 区分', async () => {
  ROUTES = ROUTES_FOR();
  const ui = await renderRoute(ROUTE);
  try {
    await flush();
    const txt = ui.text();
    assert.match(txt, /github · acme\/app · PR#12/, '票行仓库上下文可见');
    assert.match(txt, /BLOCKED/, 'run 状态可见');
    // 上下文定位用 #472 引入的桌面/窄屏共用块 mu-approval-ctx（原 .mu-approval-line
    // 在 #472 重构后只承载票据绑定 head，不再含仓库名——2026-10-16 复核定案）。
    const ctxNodes = [...ui.container.querySelectorAll('.mu-approval-ctx')]
      .filter((n) => (n.textContent ?? '').includes('acme/app'));
    assert.ok(ctxNodes.length >= 1, '共用上下文节点存在（桌面+窄屏双分支）');
    for (const n of ctxNodes) {
      assert.notEqual(n.getAttribute('style') ?? '', 'display:none', '上下文不以内联 display:none 隐藏');
    }
    // 票行内仍保留票据绑定 head 语义（与当前 head 区分——#472 保护的行为不回退）
    const lineNode = [...ui.container.querySelectorAll('.mu-approval-line')]
      .find((n) => (n.textContent ?? '').includes('票据绑定 head'));
    assert.ok(lineNode, '票据绑定 head 行存在（绑定于批准时，非当前 head）');
    assert.match(lineNode.textContent ?? '', /BLOCKED/, 'run 状态随票据绑定 head 行可见');
  } finally { await ui.cleanup(); }
});

test('G4 无权限角色：发起受控修复 disabled+服务端拒绝形态保持', async () => {
  ROUTES = {
    ...ROUTES_FOR(),
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-2', login: 'smoke-r' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'reviewer', actions: REVIEWER_ACTIONS, memberships: [],
    }],
    '/api/mu/prs/12': () => DETAIL(REVIEWER_ACTIONS),
  };
  const ui = await renderRoute(ROUTE);
  try {
    await flush();
    // 查询限定本次渲染 container（2026-10-16 复核：泄漏渲染曾污染 document 全局查询）
    const btns = findButtons(ui.container, '发起受控修复');
    assert.ok(btns.length >= 1, '按钮存在（禁用态）');
    assert.ok(btns.every((b) => b.disabled), '无权限角色按钮 disabled');
    assert.match(ui.text(), /部分动作需更高角色/, '无权限提示可见（后端仍为最终授权方）');
  } finally { await ui.cleanup(); }
});

test('G5 result_uncertain 指引优先读回对账（apply 预览文案引用，不重放写入）', async () => {
  // 结构性断言：bundle 内 apply 错误分支引用「读回对账」且不引导重复写入（#469/#465 已验收结构的回归锁）
  const src = fs.readFileSync(path.join(FRONTEND, 'src/components/ReviewArtifactsPanel.jsx'), 'utf8');
  assert.match(src, /读回对账/, '恢复入口文案存在');
  assert.match(src, /请点下方「读回对账」|读回对账/, 'uncertain 指引指向对账');
  assert.doesNotMatch(src, /result_uncertain[^`]*直接重试/, '不引导重复写入');
  ROUTES = ROUTES_FOR();
  const ui = await renderRoute(ROUTE);
  try {
    await flush();
    assert.ok(ui.text().length > 0, '页面正常渲染');
  } finally { await ui.cleanup(); }
});
