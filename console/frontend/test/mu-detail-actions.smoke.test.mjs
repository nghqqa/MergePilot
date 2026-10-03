// mu-detail-actions.smoke.test.mjs — PR-5（MU PR 详情动作按钮安全化）冒烟：
// 1) 防双击：busy 锁下双击只发一次 POST，进行中 loading 文案+disabled，完成后状态恢复；
// 2) my_permissions 控制：缺 decide_review 时按钮 disabled 且 title 含所需角色说明；
// 3) 网络失败：人话文案 + 按钮恢复可点（可重试）；
// 4) 401/403/409 错误分形：登录过期 / 角色无权 / 后端 note 展示。
// 复用 pages-smoke 基建（esbuild 打包真实 App + fetch 路由 mock）；
// 交互用 react-dom + happy-dom 真实 DOM（react-test-renderer 无法承载 antd 点击副作用）。
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
// POST 计数：按 pathname 累加（防双击断言用）
let postCounts = {};
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const method = String(init?.method ?? 'GET').toUpperCase();
  const route = ROUTES[u.pathname];
  if (method === 'POST') postCounts[u.pathname] = (postCounts[u.pathname] ?? 0) + 1;
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const out = route(u, init);
  if (out instanceof Promise) return out; // 延迟受控响应（防双击/进行中断言用）
  const [status, body] = out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-actions-smoke');
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

const COMMON = {
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, {
    service: 'console', data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } },
  }],
};

// PR 详情标准夹具：pr_id=pr-1，编号 4242；my_permissions 由用例给定
const DETAIL = (actions) => [200, {
  pull_request: { pr_id: 'pr-1', provider_pr_number: 4242, head_sha: 'ab'.repeat(20),
    branch_protection_status: 'known_clean', title: 'fix' },
  review_records: [],
  latest_run: { run_id: 'run-1', status: 'COMPLETED', architecture_version: 'v2',
    review_mode: 'evidence_only', review_verdict: 'no_blocking_findings',
    verification_verdict: 'passed', tests_status: 'passed', merge_eligibility: 'eligible' },
  my_permissions: { actions },
}];

const BASE_ROUTES = (actions) => ({
  ...COMMON,
  '/api/mu/session': () => [200, {
    user: { user_id: 'u-1', login: 'smoke1' },
    tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
    role: actions.includes('decide_review') ? 'maintainer' : 'reviewer',
    actions, memberships: [],
  }],
  '/api/mu/repositories': () => [200, { repositories: [
    { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001' }] }],
  '/api/mu/prs/4242': () => DETAIL(actions),
  '/api/mu/runs': () => [200, { runs: [] }],
});

// react-dom 渲染进 happy-dom 真实 DOM——支持 antd 按钮真实点击
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
      const btn = findButtons(container, label)[0];
      assert.ok(btn, `可点击目标：${label}`);
      btn.click();
    },
    cleanup: async () => {
      if (root) await act(async () => root.unmount());
      container.remove();
    },
  };
}

// 找可见文本匹配的按钮（antd 两字中文标签自动插空格——比对前去空白）
function findButtons(container, label) {
  const needle = label.replace(/\s+/g, '');
  return [...container.querySelectorAll('button')]
    .filter((b) => (b.textContent ?? '').replace(/\s+/g, '').includes(needle));
}
// disabled 按钮的 title 取自身或外层带 title 的包裹层（无权限时外包 span 承载 hover 说明）
function titleOf(btn) {
  if (btn.getAttribute('title')) return btn.getAttribute('title');
  const wrap = btn.closest('[title]');
  return wrap ? wrap.getAttribute('title') ?? '' : '';
}
const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

// ── 1. 防双击 + 进行中 loading + 完成恢复 ──
test('双击只发一次 POST：进行中 loading 文案+disabled，200 后按钮恢复', async () => {
  postCounts = {};
  let resolveReview = null;
  ROUTES = {
    ...BASE_ROUTES(['read_pull_request', 'request_review', 'decide_review', 'request_repair']),
    '/api/mu/prs/pr-1/review': () => new Promise((resolve) => { resolveReview = resolve; }),
  };
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    assert.ok(page.text().includes('触发只读审查'), '动作按钮区上屏');
    await act(async () => {
      page.clickButton('触发只读审查');
      page.clickButton('触发只读审查'); // 第二击在 busy 同步锁生效时到达
    });
    await flush();
    assert.equal(postCounts['/api/mu/prs/pr-1/review'], 1, '双击只发一次 POST');
    const flying = page.text();
    assert.ok(flying.includes('审查发起中'), '进行中 loading 文案上屏');
    const flyingBtn = findButtons(domWindow.document, '触发只读审查')[0]
      ?? findButtons(domWindow.document, '审查发起中')[0];
    assert.ok(flyingBtn, '进行中按钮在 DOM');
    assert.equal(flyingBtn.disabled, true, '进行中按钮 disabled');
    // 完成响应
    await act(async () => {
      resolveReview(new Response(JSON.stringify({ ok: true, job_id: 'j-1', state: 'queued' }),
        { status: 200, headers: { 'content-type': 'application/json' } }));
    });
    await flush();
    const done = page.text();
    assert.ok(done.includes('已发起只读审查'), '成功文案上屏（指向管线面板，不谎称已完成）');
    const restored = findButtons(domWindow.document, '触发只读审查')[0];
    assert.ok(restored, '完成后按钮回到初始文案');
    assert.equal(restored.disabled, false, '完成后按钮恢复可点');
    assert.ok(!done.includes('审查发起中'), 'loading 文案撤下');
  } finally { await page.cleanup(); }
});

// ── 2. my_permissions 控制：缺 decide_review → disabled + title 角色说明 ──
test('my_permissions 缺 decide_review：审批通过/驳回/受控修复 disabled 且 title 含角色说明', async () => {
  postCounts = {};
  ROUTES = BASE_ROUTES(['read_pull_request', 'request_review']); // reviewer：可审查、不可决策/修复
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    const reviewBtn = findButtons(domWindow.document, '触发只读审查')[0];
    assert.ok(reviewBtn, '审查按钮在屏');
    assert.equal(reviewBtn.disabled, false, '有 request_review：审查按钮可点');
    assert.ok(titleOf(reviewBtn).includes('不写 GitHub'), '可点按钮 title 说明动作语义（只读审查）');
    for (const label of ['审批通过', '驳回', '发起受控修复']) {
      const btn = findButtons(domWindow.document, label)[0];
      assert.ok(btn, `${label} 按钮在屏（禁用而非隐藏，布局稳定）`);
      assert.equal(btn.disabled, true, `${label} 缺权限时 disabled`);
      assert.ok(titleOf(btn).includes('maintainer'), `${label} title 含所需角色说明`);
      assert.ok(titleOf(btn).includes('无权'), `${label} title 含无权说明`);
    }
  } finally { await page.cleanup(); }
  // auditor：四个动作全禁用（不可误点）
  ROUTES = BASE_ROUTES(['read_pull_request']);
  const page2 = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    for (const label of ['触发只读审查', '审批通过', '驳回', '发起受控修复']) {
      const btn = findButtons(domWindow.document, label)[0];
      assert.ok(btn && btn.disabled === true, `auditor 下 ${label} disabled`);
    }
  } finally { await page2.cleanup(); }
});

// ── 3. 网络失败：人话文案 + 按钮恢复可点（重试再发） ──
test('网络失败：显示「网络失败——请重试」且按钮恢复可点，可再次发起', async () => {
  postCounts = {};
  ROUTES = {
    ...BASE_ROUTES(['read_pull_request', 'request_review']),
    '/api/mu/prs/pr-1/review': () => { throw new TypeError('fetch failed'); },
  };
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    await act(async () => { page.clickButton('触发只读审查'); });
    await flush();
    assert.ok(page.text().includes('网络失败——请重试'), '网络失败人话文案上屏');
    assert.ok(findButtons(domWindow.document, '重试').length > 0, '失败提示附「重试」入口');
    const btn = findButtons(domWindow.document, '触发只读审查')[0];
    assert.ok(btn && btn.disabled === false, '失败后按钮恢复可点');
    assert.ok(!page.text().includes('审查发起中'), 'loading 文案已撤下');
    await act(async () => { page.clickButton('触发只读审查'); }); // 再次点击 → 允许重发
    await flush();
    assert.equal(postCounts['/api/mu/prs/pr-1/review'], 2, '失败后可重试（第二次 POST 已发出）');
  } finally { await page.cleanup(); }
});

// ── 4. 401/403 错误分形 ──
test('401 提示登录过期；403 提示当前角色无权（按钮均恢复可点）', async () => {
  postCounts = {};
  ROUTES = {
    ...BASE_ROUTES(['read_pull_request', 'request_review']),
    '/api/mu/prs/pr-1/review': () => [401, { error: { reason: 'unauthorized' } }],
  };
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    await act(async () => { page.clickButton('触发只读审查'); });
    await flush();
    assert.ok(page.text().includes('登录已过期，请刷新页面'), '401 文案上屏');
    assert.equal(findButtons(domWindow.document, '触发只读审查')[0].disabled, false, '401 后按钮恢复');

    ROUTES['/api/mu/prs/pr-1/review'] = () => [403, { error: { reason: 'forbidden' } }];
    await act(async () => { page.clickButton('触发只读审查'); });
    await flush();
    assert.ok(page.text().includes('当前角色无权执行该动作'), '403 文案上屏');
    assert.equal(findButtons(domWindow.document, '触发只读审查')[0].disabled, false, '403 后按钮恢复');
  } finally { await page.cleanup(); }
});

// ── 5. 409 冲突：展示后端 note 原文 ──
test('409 冲突：展示后端 note 原文，按钮恢复可点', async () => {
  postCounts = {};
  ROUTES = {
    ...BASE_ROUTES(['read_pull_request', 'request_review', 'decide_review']),
    '/api/mu/prs/pr-1/decision': () => [409, { error: { reason: 'decision_conflict' },
      note: '该 PR 已有更新的 head——请刷新页面后重试' }],
  };
  const page = await renderRoute('/mu/repos/acme/app/pr/4242');
  try {
    await act(async () => { page.clickButton('驳回'); });
    await flush();
    const text = page.text();
    assert.ok(text.includes('操作未完成（HTTP 409）'), '409 状态明示');
    assert.ok(text.includes('该 PR 已有更新的 head——请刷新页面后重试'), '后端 note 原文上屏');
    assert.equal(findButtons(domWindow.document, '驳回')[0].disabled, false, '409 后按钮恢复可点');
  } finally { await page.cleanup(); }
});
