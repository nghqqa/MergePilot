// login-invite.smoke.test.mjs — PR #348 登录/邀请 UX 行为级冒烟（渲染真实 App）。
// 覆盖：已登录 /login?invite= 参数保留与传参；邀请链接 scope 四分支（纯函数行为）；
//       multiuser 残留演示标记在守卫层阻断（不依赖登录页清理）；legacy 演示兼容；
//       providers 配置失败不回退备用登录；multiuser 登录页不渲染操作员表单。
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
import { inviteLinkScope } from '../src/invite-link.js';

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
globalThis.sessionStorage ??= domWindow.sessionStorage;
globalThis.getComputedStyle ??= domWindow.getComputedStyle.bind(domWindow);
globalThis.matchMedia ??= domWindow.matchMedia.bind(domWindow);
globalThis.requestAnimationFrame ??= domWindow.requestAnimationFrame.bind(domWindow);
globalThis.cancelAnimationFrame ??= domWindow.cancelAnimationFrame.bind(domWindow);

const DEMO_KEY = 'mp-console-demo-preview';
const SESSION_AUTHED = { user: { name: 'smoke-admin' }, repos: [], role: 'platform_admin' };
let ROUTES = {};
let calls = [];
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  calls.push(u.pathname + u.search);
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.login-invite-smoke');
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

const HEALTH = () => [200, { service: 'console', data_mode: 'multiuser',
  sources: { primary: 'multiuser', multiuser: { available: true } }, mu_schema_ready: true }];

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

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

test('1. 已登录打开 /login?invite=：不丢参、受邀按钮直传 invite', async () => {
  domWindow.location.href = 'http://smoke.local/login?invite=inv-abc';
  ROUTES = {
    '/api/auth/session': () => [200, SESSION_AUTHED], // 已登录
    '/api/health': HEALTH,
    '/api/mu/auth/providers': () => [200, { github: { configured: true, callback_url: 'https://mp.example.com/api/mu/auth/oauth/github/callback' }, fixture: { configured: false } }],
    '/api/mu/auth/oauth/github/start': (u) => [200, { authorize_url: 'https://github.com/login/oauth/authorize?invite_bound=1' }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/login?invite=inv-abc');
  try {
    const text = json();
    assert.ok(!text.includes('"api/mu/runs'), '未跳转 /pending（参数未丢）');
    assert.ok(text.includes('使用受邀的 GitHub 账号登录'), '受邀按钮文案上屏');
    assert.ok(text.includes('你当前已登录，继续将通过 GitHub 验证受邀身份'), '已登录提示上屏（不称“其他身份”）');
    assert.ok(text.includes('仅受邀成员可登录'), '邀请制提示上屏');
    // 行为：点击受邀按钮 → start 携带原样 invite 参数
    const btn = renderer.root.find((n) => n.props?.onClick && n.props?.children === '使用受邀的 GitHub 账号登录');
    await act(async () => { await btn.props.onClick(); });
    await flush();
    assert.ok(calls.some((c) => c === '/api/mu/auth/oauth/github/start?invite=inv-abc'), `start 携带 invite 参数：${calls.join('|')}`);
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('2. 邀请链接 scope 四分支（缺失/回环/HTTP/HTTPS）', () => {
  const missing = inviteLinkScope(null);
  assert.equal(missing.scope, 'unconfigured');
  assert.ok(!missing.shareable && missing.note.includes('无法生成邀请链接'), '缺失：不生成链接');
  const loopback = inviteLinkScope('http://127.0.0.1:48500/api/mu/auth/oauth/github/callback');
  assert.equal(loopback.scope, 'loopback');
  assert.equal(loopback.origin, 'http://127.0.0.1:48500');
  assert.ok(!loopback.shareable && loopback.note.includes('仅本机可用'), '回环：标注仅本机可用');
  const httpIntranet = inviteLinkScope('http://192.168.10.5/api/mu/auth/oauth/github/callback');
  assert.equal(httpIntranet.scope, 'http');
  assert.ok(!httpIntranet.shareable && httpIntranet.note.includes('不符合推荐的对外 HTTPS'), 'HTTP：明示不符合推荐配置');
  const httpsPub = inviteLinkScope('https://mp.example.com/api/mu/auth/oauth/github/callback');
  assert.equal(httpsPub.scope, 'https');
  assert.ok(httpsPub.shareable && httpsPub.origin === 'https://mp.example.com', 'HTTPS：可共享');
  assert.ok(httpsPub.note.includes('未探测公网可达性'), '不宣称已验证公网可达');
  const bad = inviteLinkScope('ftp://x/cb');
  assert.equal(bad.scope, 'invalid');
});

test('3. multiuser 残留演示标记：守卫层阻断放行并清理标记', async () => {
  domWindow.location.href = 'http://smoke.local/multiuser';
  globalThis.sessionStorage.setItem(DEMO_KEY, '1'); // 残留标记
  ROUTES = {
    '/api/auth/session': () => [401, { error: { reason: 'not_authenticated' }, capabilities: { legacy_login: false, multiuser: true } }],
    '/api/health': HEALTH,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/auth/providers': () => [200, { github: { configured: true, callback_url: 'https://mp.example.com/cb' } }],
  };
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('使用 GitHub 登录'), 'multiuser 下不被残留标记放行（渲染登录页）');
    assert.ok(text.includes('仅受邀成员可登录'), '邀请制提示上屏');
    assert.ok(!text.includes('只读演示预览'), '演示芯片不出现（守卫层阻断，非登录页清理）');
    assert.equal(globalThis.sessionStorage.getItem(DEMO_KEY), null, '残留标记已被登录页清理');
  } finally { await act(async () => { renderer.unmount(); }); }
  globalThis.sessionStorage.removeItem(DEMO_KEY);
});

test('4. legacy 形态演示兼容保留（不影响真实认证权限）', async () => {
  domWindow.location.href = 'http://smoke.local/pending';
  globalThis.sessionStorage.setItem(DEMO_KEY, '1');
  ROUTES = {
    '/api/auth/session': () => [401, { error: { reason: 'not_authenticated' }, capabilities: { legacy_login: true, multiuser: false } }],
    '/api/health': () => [200, { service: 'console', data_mode: 'fixture' }],
    '/api/runs': () => [200, { runs: [] }],
  };
  const { renderer, json } = await renderRoute('/pending');
  try {
    const text = json();
    assert.ok(text.includes('只读演示预览'), 'legacy 形态保留既有演示策略（标记放行浏览）');
  } finally { await act(async () => { renderer.unmount(); }); }
  globalThis.sessionStorage.removeItem(DEMO_KEY);
});

test('5. providers 获取失败：准确提示+重试，不回退操作员表单/演示', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    '/api/auth/session': () => [401, { error: { reason: 'not_authenticated' }, capabilities: { legacy_login: false, multiuser: true } }],
    '/api/health': HEALTH,
    '/api/mu/auth/providers': () => [500, { error: { reason: 'boom' } }],
  };
  const { renderer, json } = await renderRoute('/login');
  try {
    const text = json();
    assert.ok(text.includes('登录配置获取失败'), '配置失败提示上屏');
    assert.ok(text.includes('不会回退到其他入口'), '明示不回退备用登录');
    assert.ok(text.includes('重试获取登录配置'), '重试入口上屏');
    assert.ok(!text.includes('操作员账号'), '不回退操作员表单');
    assert.ok(!text.includes('以只读演示进入'), '不回退演示入口');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('6. multiuser 未认证普通 /login：无操作员表单，仅 GitHub 主按钮+邀请制提示', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    '/api/auth/session': () => [401, { error: { reason: 'not_authenticated' }, capabilities: { legacy_login: false, multiuser: true } }],
    '/api/health': HEALTH,
    '/api/mu/auth/providers': () => [200, { github: { configured: true, callback_url: 'https://mp.example.com/cb' } }],
  };
  const { renderer, json } = await renderRoute('/login');
  try {
    const text = json();
    assert.ok(text.includes('使用 GitHub 登录'), 'GitHub 主按钮上屏');
    assert.ok(text.includes('仅受邀成员可登录，需要访问权限请联系管理员'), '邀请制提示上屏');
    assert.ok(!text.includes('操作员账号'), '操作员密码表单不渲染');
    assert.ok(!text.includes('或使用操作员账号'), '分隔文案不渲染');
    assert.ok(!text.includes('以只读演示进入'), '演示入口不渲染');
  } finally { await act(async () => { renderer.unmount(); }); }
});
