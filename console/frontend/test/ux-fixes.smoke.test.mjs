// ux-fixes.smoke.test.mjs — 前端 UX 修复轮冒烟：
//  1) 组织与接入页渐进 onboarding：bind/waiting/review 各状态单一主 CTA；
//  2) PR 列表键盘可访问（真实 button + aria-label 含 PR 编号/仓库/head SHA）；
//  3) raw action id 收进"技术详情"、人话标签上屏；
//  4) RAG 灌入：确认门 + 失败保留输入 + 人话错误 + 技术详情（不含堆栈/密钥）；
//  5) RAG repo 选择器：MU 已绑定仓库自动选中，MU 不可达降级自由输入。
// 复用 pages-smoke 基建（esbuild 真打包 + react-test-renderer + happy-dom）。
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

const SESSION = { user: { name: 'smoke' }, repos: [] };
let ROUTES = {};
let calls = [];
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  calls.push(`${input?.method ?? 'GET'} ${u.pathname}${u.search}`);
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.ux-fixes-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\nexport { App };`);
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  return mod.App ?? mod.default?.App ?? mod.default ?? mod;
}

const COMMON = {
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, { service: 'console', data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } } }],
};

async function renderRoute(route) {
  const App = await loadApp();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
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
// 按子节点顺序拼接全部文本（相邻字符串节点在 JSON 里是数组元素，直接 includes 会漏）
function allText(json) {
  let t = '';
  walk(json, (nd) => { if (typeof nd === 'string') t += nd; });
  return t;
}
function countPrimaryButtons(json) {
  let n = 0;
  walk(json, (nd) => {
    const cn = nd.props?.className;
    if (typeof cn === 'string' && cn.includes('ant-btn-primary') && !cn.includes('ant-btn-dangerous')) n++;
  });
  return n;
}
function findButtonByText(json, text) {
  let hit = null;
  walk(json, (nd) => {
    if (hit || nd.type !== 'button') return;
    let t = '';
    walk(nd, (x) => { if (typeof x === 'string') t += x; });
    if (t.includes(text)) hit = nd;
  });
  return hit;
}
function findByAriaLabel(json, prefix) {
  let hit = null;
  walk(json, (nd) => {
    const al = nd.props?.['aria-label'];
    if (!hit && typeof al === 'string' && al.startsWith(prefix)) hit = nd;
  });
  return hit;
}

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

const MU_MAINTAINER = {
  user: { user_id: 'u-1', login: 'alice' },
  tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
  role: 'maintainer',
  actions: ['read_repository', 'read_pull_request', 'request_review', 'decide_review', 'request_repair', 'manage_repository_binding'],
};

const GH_APP_OK = {
  '/api/mu/github/app/status': () => [200, { configured: true, app_id: 999, permissions: ['contents:read'], events: ['pull_request'] }],
  '/api/mu/github/installations': () => [200, { installations: [{ installation_id: 7001, account_login: 'acme', suspended: false, revoked: false }] }],
};

test('组织与接入：bind 阶段单一主 CTA（前往选择仓库并绑定），raw action id 收进技术详情', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, MU_MAINTAINER],
    '/api/mu/auth/providers': () => [200, { github: { configured: true } }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/repositories': () => [200, { repositories: [] }],
    ...GH_APP_OK,
  };
  calls = [];
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('前往选择仓库并绑定'), 'bind 阶段主 CTA 上屏');
    assert.ok(!text.includes('使用 GitHub 登录'), '已登录不再显示登录盒');
    assert.ok(text.includes('管理仓库绑定'), '权限动作人话标签上屏');
    assert.ok(text.includes('manage_repository_binding'), 'raw action id 保留在技术详情中');
    assert.equal(countPrimaryButtons(renderer.toJSON()), 1, 'bind 阶段全页仅 1 个主 CTA');
    assert.ok(!text.includes('请求失败') && !text.includes('unexpected'), '零错误态');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('组织与接入：waiting 阶段（已绑定仓库、无 PR）显示等待同步与检查 CTA', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, MU_MAINTAINER],
    '/api/mu/auth/providers': () => [200, { github: { configured: true } }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: '9001', binding_id: 'b-1', installation_state: 'active' },
    ] }],
    ...GH_APP_OK,
    '/api/mu/prs': () => [200, { pull_requests: [] }],
  };
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('检查 PR 同步'), 'waiting 阶段主 CTA 上屏');
    assert.ok(text.includes('该仓库还没有 PR'), '空 PR 人话空态上屏');
    assert.ok(text.includes('等待 PR 同步') || text.includes('webhook'), 'webhook 等待说明上屏');
    assert.equal(countPrimaryButtons(renderer.toJSON()), 1, 'waiting 阶段全页仅 1 个主 CTA');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('组织与接入：review 阶段 PR 列表为真实按钮（aria-label 含编号/仓库/head），主 CTA 打开最新 PR', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, MU_MAINTAINER],
    '/api/mu/auth/providers': () => [200, { github: { configured: true } }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: '9001', binding_id: 'b-1', installation_state: 'active' },
    ] }],
    ...GH_APP_OK,
    '/api/mu/prs': () => [200, { pull_requests: [
      { pr_id: 'p-1', provider_pr_number: 101, head_sha: 'aaa1bbb2c3d4e5f6', branch_protection_status: 'known_clean', updated_at: '2026-09-28T10:00:00Z' },
      { pr_id: 'p-2', provider_pr_number: 102, head_sha: 'bbb2', branch_protection_status: 'unknown', updated_at: '2026-09-28T11:00:00Z' },
    ] }],
    '/api/mu/prs/p-2': () => [200, { pull_request: { pr_id: 'p-2', repo_owner: 'acme', repo_name: 'app', head_sha: 'bbb2', branch_protection_status: 'unknown' }, review_records: [] }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('打开最新 PR（#102）'), '主 CTA 指向最新 PR');
    const prBtn = findByAriaLabel(renderer.toJSON(), '打开 PR #101');
    assert.ok(prBtn, 'PR 行内存在可键盘触达的真实按钮');
    assert.match(prBtn.props['aria-label'], /acme\/app/, 'aria-label 含仓库');
    assert.match(prBtn.props['aria-label'] ?? '', /head aaa1bbb2c3d4/, 'aria-label 含 head SHA');
    assert.ok(text.includes('aaa1bbb2c3d4'), 'head SHA 列保留');
    assert.ok(text.includes('受保护 · 已验证'), 'branch protection 人话标签上屏');
    assert.equal(countPrimaryButtons(renderer.toJSON()), 1, 'review 阶段全页仅 1 个主 CTA');
    // 键盘行为：真实 <button>（Enter/Space 原生触发）——触发 onClick 应加载详情
    const prBtn102 = findByAriaLabel(renderer.toJSON(), '打开 PR #102');
    await act(async () => { prBtn102.props.onClick(); });
    for (let i = 0; i < 10; i++) await act(async () => { await Promise.resolve(); });
    assert.ok(calls.some((c) => c.includes('/api/mu/prs/p-2')), '按钮触发 openPr 行为（既有端点不变）');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('RAG 灌入：确认门开启前禁用；失败保留输入、人话报错、技术详情含 scope_denied 且无堆栈', async () => {
  // 说明：react-test-renderer 无真实 DOM，rc-input 的 onChange 依赖 inputRef 不可驱动——
  // 改用 MU 已绑定仓库选择器自动填充 repo/branch（同一受控 state），验证同一确认门与失败恢复链。
  ROUTES = {
    ...COMMON,
    '/api/rag-trial/status': () => [200, { service_state: 'ready', models: [] }],
    '/api/rag-trial/metrics': () => [200, {}],
    '/api/mu/session': () => [200, MU_MAINTAINER],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: '9001', binding_id: 'b-1', installation_state: 'active', default_branch: 'main' },
    ] }],
    '/api/rag-trial/ingest': () => [403, { error: { reason: 'scope_denied', detail: 'repo not in allowed scopes' } }],
  };
  let ingestCalls = 0;
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    if (String(input).includes('/api/rag-trial/ingest')) ingestCalls++;
    return origFetch(input, init);
  };
  const { renderer } = await renderRoute('/rag-trial');
  try {
    let tree = renderer.toJSON();
    // 选择器自动填充后：确认前按钮禁用、复选框可用
    const checkbox = () => { const a = []; walk(tree, (nd) => { if (nd.type === 'input' && nd.props?.type === 'checkbox') a.push(nd); }); return a[0] ?? null; };
    const ingestBtn = () => { tree = renderer.toJSON(); return findButtonByText(tree, '灌入语料'); };
    assert.ok(checkbox()?.props.disabled === false, 'repo/branch 就绪后确认框可用');
    assert.ok(ingestBtn()?.props.disabled === true, '未确认时灌入按钮禁用');
    // 勾选确认 → 按钮可用 → 触发失败
    await act(async () => { checkbox().props.onChange({ target: { checked: true } }); });
    assert.ok(ingestBtn()?.props.disabled === false, '确认后灌入按钮可用');
    await act(async () => { ingestBtn().props.onClick({ preventDefault() {}, stopPropagation() {} }); });
    for (let i = 0; i < 10; i++) await act(async () => { await Promise.resolve(); });
    const text2 = allText(renderer.toJSON());
    assert.equal(ingestCalls, 1, '灌入端点恰好调用一次（进行中禁用重复提交）');
    assert.ok(text2.includes('没有这个仓库的 RAG 权限'), '失败人话说明上屏');
    assert.ok(text2.includes('scope_denied'), '技术详情含机器 reason（可展开）');
    assert.ok(!text2.includes('\n    at '), '不渲染调用堆栈');
    assert.ok(text2.includes('acme/app') && text2.includes('main'), '失败后目标输入保留');
    assert.ok(ingestBtn()?.props.disabled === true, '失败后重新要求确认（按钮回到禁用）');
  } finally {
    globalThis.fetch = origFetch;
    await act(async () => { renderer.unmount(); });
  }
});

function findAllInputs(tree, acc = []) {
  walk(tree, (nd) => { if (nd.type === 'input') acc.push(nd); });
  return acc;
}

test('RAG repo：MU 已绑定仓库时优先选择器并自动选中；断言组织与仓库摘要上屏', async () => {
  ROUTES = {
    ...COMMON,
    '/api/rag-trial/status': () => [200, { service_state: 'ready', models: [] }],
    '/api/rag-trial/metrics': () => [200, {}],
    '/api/mu/session': () => [200, MU_MAINTAINER],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: '9001', binding_id: 'b-1', installation_state: 'active', default_branch: 'main' },
    ] }],
  };
  const { renderer } = await renderRoute('/rag-trial');
  try {
    const text = allText(renderer.toJSON());
    assert.ok(text.includes('当前组织：default'), '当前租户上屏');
    assert.ok(text.includes('已绑定 1 个仓库'), '绑定仓库摘要上屏');
    assert.ok(text.includes('acme/app'), '选择器自动选中首个绑定仓库');
    assert.ok(text.includes('我确认以上灌入目标'), '灌入确认门上屏');
    assert.ok(text.includes('POST /api/rag-trial/ingest'), '技术端点收进技术详情');
  } finally { await act(async () => { renderer.unmount(); }); }
});
