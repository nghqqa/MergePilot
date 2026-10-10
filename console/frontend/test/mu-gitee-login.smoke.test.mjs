// mu-gitee-login.smoke.test.mjs — Wave-Gitee 登录页 Gitee 入口冒烟。
// 锁三件事：① providers 含 gitee.configured=true 时 Gitee 按钮上屏；
// ② providers 无 gitee 键（存量部署形状）时不渲染 Gitee 入口（不伪装可用）；
// ③ 邀请 provider 错配（GitHub start 409）时人话指引指向正确入口。
// 复用 pages-smoke 基建（bundle 名独立，防模块缓存污染）。
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
  calls.push(u.pathname + u.search);
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-gitee-smoke');
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
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

test('GL-S1 providers 含 gitee.configured=true → GitHub 主按钮 + Gitee 按钮双入口上屏', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/auth/providers': () => [200, {
      github: { configured: true, callback_url: 'http://x/cb', scope: 'read:user' },
      gitee: { configured: true, callback_url: 'http://x/cb-gitee', scope: 'user_info' },
    }],
  };
  const { renderer, json } = await renderRoute('/login');
  try {
    const text = json();
    assert.ok(text.includes('使用 GitHub 登录'), 'GitHub 主按钮上屏（行为不变）');
    assert.ok(text.includes('使用 Gitee 登录'), 'Gitee 入口由服务端配置驱动上屏');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('GL-S2 providers 无 gitee 键（存量形状）→ 不渲染 Gitee 入口（不伪装可用）', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/auth/providers': () => [200, { github: { configured: true, callback_url: 'http://x/cb', scope: 'read:user' } }],
  };
  const { renderer, json } = await renderRoute('/login');
  try {
    const text = json();
    assert.ok(text.includes('使用 GitHub 登录'), 'GitHub 主按钮上屏');
    assert.ok(!text.includes('使用 Gitee 登录'), '未配置 provider 不渲染入口（fail-closed）');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('GL-S3 邀请 provider 错配：GitHub start 409 → 指引改用 Gitee 入口（邀请未消耗）', async () => {
  domWindow.location.href = 'http://smoke.local/login?invite=11111111-1111-4111-8111-111111111111';
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/auth/providers': () => [200, {
      github: { configured: true, callback_url: 'http://x/cb', scope: 'read:user' },
      gitee: { configured: true, callback_url: 'http://x/cb-gitee', scope: 'user_info' },
    }],
    '/api/mu/auth/oauth/github/start': () => [409, { error: { reason: 'invite_provider_mismatch',
      detail: '该邀请绑定的不是 GitHub 身份' } }],
  };
  const { renderer, json } = await renderRoute('/login?invite=11111111-1111-4111-8111-111111111111');
  try {
    // 点击 GitHub 主按钮（受邀态文案=「使用受邀的 GitHub 账号登录」；带 invite 调 start）
    const label = (n) => String(Array.isArray(n.props?.children) ? n.props.children.join('') : (n.props?.children ?? ''));
    const ghBtn = renderer.root.find((n) => typeof n.props?.onClick === 'function'
      && /GitHub 账号登录|使用 GitHub 登录/.test(label(n)));
    await act(async () => { ghBtn.props.onClick(); await Promise.resolve(); });
    for (let i = 0; i < 10; i++) await act(async () => { await Promise.resolve(); });
    const text = json();
    assert.ok(calls.some((c) => c.startsWith('/api/mu/auth/oauth/github/start')), 'start 已调用（带 invite）');
    assert.ok(text.includes('该邀请绑定的是 Gitee 账号'), 'provider 错配人话指引上屏');
    assert.ok(text.includes('使用 Gitee 登录'), '指引到 Gitee 入口（可从正确入口重试）');
    assert.ok(!text.includes('invite_provider_mismatch'), '机器 reason 不反射上屏');
  } finally { await act(async () => { renderer.unmount(); }); }
});
