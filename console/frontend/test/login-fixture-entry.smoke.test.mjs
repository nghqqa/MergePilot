// login-fixture-entry.smoke.test.mjs — fixture 自助登录入口冒烟（2026-10-16）。
// 锁三件事：
//  ① providers 驱动渲染：fixture.configured=true → 「试用身份」区块渲染；
//     未披露（生产默认）→ 不渲染（不伪装可用）；
//  ② 提交契约：身份名 → POST /api/mu/auth/login {provider:'fixture', subject:'fixture:<名>'}；
//  ③ 失败如实：identity_unknown 等后端 reason 映射人话，不猜原因；
//     页面不出现部署变量名（MU_*）。
// 复用 login-source-ui 冒烟基建（bundle 名独立，防模块缓存污染）。
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

let ROUTES = {};
let calls = [];
let bodies = [];
const assigns = []; // location.assign 记录（登录成功/进入系统入口的导航断言）
try {
  domWindow.location.assign = (u) => { assigns.push(String(u)); };
} catch {
  Object.defineProperty(domWindow.location, 'assign', { value: (u) => { assigns.push(String(u)); }, configurable: true });
}
globalThis.fetch = async (input, init = {}) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  calls.push(u.pathname + u.search);
  if (init?.body) bodies.push({ path: u.pathname, body: String(init.body) });
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

let LOAD_SEQ = 0;
async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.login-fixture-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const seq = ++LOAD_SEQ;
  const entry = path.join(outDir, `entry-${seq}.mjs`);
  const bundle = path.join(outDir, `bundle-${seq}.cjs`);
  fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\nexport { App };`);
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  return mod.App ?? mod.default?.App ?? mod.default ?? mod;
}

const HEALTH = () => [200, { service: 'console', data_mode: 'multiuser',
  sources: { primary: 'multiuser', multiuser: { available: true } }, mu_schema_ready: true }];
const ANON = () => [401, { error: { reason: 'not_authenticated' }, capabilities: { legacy_login: false, multiuser: true } }];
// 试用栈形态（与 18700 实测一致）：OAuth 未配置 + fixture 开放
const TRIAL_PROVIDERS = () => [200, {
  github: { configured: false, reason: 'oauth_not_configured' },
  gitee: { configured: false, reason: 'oauth_not_configured' },
  fixture: { configured: true },
}];
// 生产形态：平台已配置、无 fixture 键
const PROD_PROVIDERS = () => [200, {
  github: { configured: true, callback_url: 'http://x/cb', scope: 'read:user' },
  gitee: { configured: true, callback_url: 'http://x/cb-gitee', scope: 'user_info' },
}];

async function renderRoute(route) {
  const App = await loadApp();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}
const flush = async () => { for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); }); };
const label = (n) => String(Array.isArray(n.props?.children) ? n.props.children.join('') : (n.props?.children ?? ''));

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

async function fillAndSubmit(renderer, name) {
  // react-test-renderer 不产真实 DOM——沿组件树自 Input 向上找 Form 的 onFinish
  // 直呼（等价校验通过后的提交路径；区块渲染断言已由 LFE-1 覆盖）。
  const input = renderer.root.find((n) => n.props?.id === 'login-fixture-name');
  assert.ok(input, 'fixture 身份输入框应存在');
  let node = input;
  while (node && typeof node.props?.onFinish !== 'function') node = node.parent;
  assert.ok(node, 'fixture Form 应带 onFinish');
  await act(async () => { node.props.onFinish({ name }); });
  await flush();
}

test('LFE-1 试用栈形态：fixture 开放 → 试用身份区块渲染；OAuth 未配置如实提示且无 OAuth 按钮', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = { '/api/auth/session': ANON, '/api/health': HEALTH, '/api/mu/auth/providers': TRIAL_PROVIDERS };
  calls = [];
  const { json } = await renderRoute('/login');
  const s = json();
  assert.ok(s.includes('以试用身份登录'), '试用身份登录按钮应渲染');
  assert.ok(s.includes('试用身份名'), '字段 label 应渲染');
  assert.ok(s.includes('GitHub 登录暂未开放'), 'OAuth 未配置应如实提示');
  assert.ok(!s.includes('使用 GitHub 登录'), '未配置平台不渲染 OAuth 按钮（不伪装可用）');
  assert.ok(!/MU_ALLOW_FIXTURE_LOGIN/.test(s), '页面不出现部署变量名');
});

test('LFE-2 生产形态：providers 无 fixture 披露 → 区块不渲染', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = { '/api/auth/session': ANON, '/api/health': HEALTH, '/api/mu/auth/providers': PROD_PROVIDERS };
  const { json } = await renderRoute('/login');
  const s = json();
  assert.ok(!s.includes('以试用身份登录'), '未披露 fixture 时不得渲染试用区块');
  assert.ok(s.includes('使用 GitHub 登录'), '已配置平台按钮照常');
});

test('LFE-3 提交契约：身份名 → POST /api/mu/auth/login subject=fixture:<名>；成功后刷新会话', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    '/api/auth/session': ANON, '/api/health': HEALTH, '/api/mu/auth/providers': TRIAL_PROVIDERS,
    '/api/mu/auth/login': () => [200, { ok: true, csrf: 'c1', role: 'maintainer',
      user: { user_id: 'u1', login: 'trial-maintainer' } }],
  };
  calls = []; bodies = []; assigns.length = 0;
  const { renderer } = await renderRoute('/login');
  await fillAndSubmit(renderer, 'trial-maintainer');
  const hit = bodies.find((b) => b.path === '/api/mu/auth/login');
  assert.ok(hit, '登录 POST 应发出');
  const parsed = JSON.parse(hit.body);
  assert.equal(parsed.provider, 'fixture');
  assert.equal(parsed.subject, 'fixture:trial-maintainer');
  assert.ok(assigns.includes('/'), '登录成功应直接进入系统（location.assign(\'/\')）');
  const s2 = renderer.toJSON();
  const text = JSON.stringify(s2);
  assert.ok(!text.includes('身份登录未完成'), '成功路径不显示失败文案');
});

test('LFE-4 失败如实：identity_unknown → 「该身份未由管理员预置…」，不猜其他原因', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    '/api/auth/session': ANON, '/api/health': HEALTH, '/api/mu/auth/providers': TRIAL_PROVIDERS,
    '/api/mu/auth/login': () => [401, { error: { reason: 'identity_unknown' } }],
  };
  calls = []; bodies = []; assigns.length = 0;
  const { renderer } = await renderRoute('/login');
  await fillAndSubmit(renderer, 'nobody');
  const s = json0(renderer);
  assert.ok(s.includes('该身份未由管理员预置'), '未知身份文案应上屏');
  assert.equal(assigns.length, 0, '失败路径不导航');
});

test('LFE-5 已登录用户访问 /login（非受邀）→ 提供 「进入系统」 入口，不形成死端', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  const AUTHED = () => [200, { user: { login: 'trial-maintainer', login_type: 'fixture' },
    expires_at: null, capabilities: { legacy_login: false, multiuser: true } }];
  ROUTES = { '/api/auth/session': AUTHED, '/api/health': HEALTH, '/api/mu/auth/providers': TRIAL_PROVIDERS };
  calls = []; assigns.length = 0;
  const { renderer } = await renderRoute('/login');
  const s = json0(renderer);
  assert.ok(s.includes('你当前已登录'), '已登录提示应上屏');
  assert.ok(s.includes('进入系统'), '应提供进入系统入口');
  const btn = renderer.root.find((n) => typeof n.props?.onClick === 'function'
    && label0(n).includes('进入系统'));
  await act(async () => { btn.props.onClick(); });
  assert.ok(assigns.includes('/'), '点击进入系统应导航到 /');
});
function label0(n) { return String(Array.isArray(n.props?.children) ? n.props.children.join('') : (n.props?.children ?? '')); }
function json0(renderer) { return JSON.stringify(renderer.toJSON()); }
