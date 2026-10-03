// mu-oauth-page.smoke.test.mjs — Wave 2A 多用户登录盒冒烟。
// 复用 pages-smoke 基建：mock /api/mu/session（401 无会话）+ /api/mu/auth/providers，
// 锁"GitHub 登录按钮上屏 + 配置缺失诚实提示 + 未邀请错误映射"，零错误态。
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
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-oauth-smoke');
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

test('登录盒：GitHub 按钮上屏 + 配置缺失 fail-closed 提示（无会话态）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/auth/providers': () => [200, { github: { configured: false, reason: 'oauth_not_configured' } }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('使用 GitHub 登录'), 'GitHub 登录按钮上屏');
    assert.ok(text.includes('GitHub OAuth 未配置'), '配置缺失诚实提示（configured:false fail-closed）');
    assert.ok(calls.some((c) => c.startsWith('/api/mu/auth/providers')), '拉取 providers 状态');
    assert.ok(!text.includes('请求失败'), '配置缺失不是错误态');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('登录盒：已配置态按钮可点 + mu_login_error 未邀请映射', async () => {
  domWindow.location.href = 'http://smoke.local/multiuser?mu_login_error=not_invited';
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/auth/providers': () => [200, { github: { configured: true, callback_url: 'http://x/cb', scope: 'read:user' } }],
  };
  const { renderer, json } = await renderRoute('/multiuser?mu_login_error=not_invited');
  try {
    const text = json();
    assert.ok(text.includes('使用 GitHub 登录'), '已配置态按钮上屏');
    // 审计 G-1（2026-10-03）：not_invited 无会话态错误此前在 OnboardingPanel（仅已会话
    // 渲染）内且要求 ob.stage==='login'（仅无会话）——互斥不可达。现锁无会话登录卡内
    // 人话映射 + 下一步指引（联系管理员 + GitHub 数字 user id 公开查询路径）真实上屏。
    assert.ok(text.includes('身份未被邀请'), '未邀请错误人话映射上屏');
    assert.ok(text.includes('请联系管理员'), '下一步指引：联系管理员上屏');
    assert.ok(text.includes('api.github.com/users/'), '数字 user id 公开查询路径上屏');
    assert.ok(!text.includes('mu_login_error'), '原始机器参数不反射上屏');
  } finally { await act(async () => { renderer.unmount(); }); }
});
