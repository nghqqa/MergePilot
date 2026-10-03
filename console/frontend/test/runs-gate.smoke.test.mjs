// runs-gate.smoke.test.mjs — /runs/:packId 数据源门控回归锁（审计 P1-8）。
// 根因：/runs 列表经 RunsHistoryRoute 门控（仅 snapshot 源可用），而 /runs/:packId
// 直连 RunDetailPage 无门控——MU 部署下已认证成员输 URL 可读取部署级全局
// snapshot 证据包（不按租户隔离）。本测试渲染真实 App（MemoryRouter + esbuild bundle）：
//   - MU 源（primary=multiuser）→ /runs/:packId 渲染门控文案，且 0 次 /api/runs/* 请求
//   - snapshot 源 → RunDetailPage 正常挂载并发起 /api/runs/:id 请求（行为不变）
// 运行：cd console/frontend && node --test test/runs-gate.smoke.test.mjs
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

let App;
let clearRuntimeConfigCache;

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.runs-gate-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry,
    `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\n` +
    `import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};\n` +
    `export { App, clearRuntimeConfigCache };`);
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  App = mod.App ?? mod.default?.App ?? mod.default;
  clearRuntimeConfigCache = mod.clearRuntimeConfigCache;
}

async function renderRoute(route) {
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}

const MU_HEALTH = { service: 'console', data_mode: 'live',
  sources: { primary: 'multiuser', multiuser: { available: true } } };
const SNAPSHOT_HEALTH = { service: 'console', data_mode: 'snapshot', sources: {} };

test('MU 源下 /runs/:packId 渲染门控文案且零 /api/runs/* 请求', async () => {
  await loadApp();
  clearRuntimeConfigCache();
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, MU_HEALTH],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/runs/demo-pack-1');
  try {
    const out = json();
    // 注：subject 与正文是相邻文本节点，JSON 序列化后不相邻——用各门独有的完整短语断言。
    assert.match(out, /不提供 run 证据包详情/);
    assert.doesNotMatch(out, /不提供 run 级全量历史/);
    assert.equal(calls.filter((c) => c.startsWith('/api/runs/')).length, 0,
      `MU 源下不得发起 snapshot 请求，实际: ${calls.join(', ')}`);
  } finally {
    renderer.unmount();
  }
});

test('MU 源下 /runs 列表门控行为保持（共享门组件回归）', async () => {
  clearRuntimeConfigCache();
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, MU_HEALTH],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/runs');
  try {
    assert.match(json(), /不提供 run 级全量历史/);
    assert.equal(calls.filter((c) => c.startsWith('/api/runs/')).length, 0);
  } finally {
    renderer.unmount();
  }
});

test('snapshot 源下 /runs/:packId 正常挂载 RunDetailPage 并发起请求（行为不变）', async () => {
  clearRuntimeConfigCache();
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, SNAPSHOT_HEALTH],
    // run 详情故意 404：本用例只锁"门已放行、页面挂载并发起请求"，
    // 数据形状渲染属 RunDetailPage 自身测试域。
    '/api/runs/demo-pack-1': () => [404, { error: { reason: 'not_found' } }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/runs/demo-pack-1');
  try {
    assert.ok(calls.some((c) => c.startsWith('/api/runs/demo-pack-1')),
      `snapshot 源下应发起 run 详情请求，实际: ${calls.join(', ')}`);
    assert.doesNotMatch(json(), /为 snapshot 取证视图/);
  } finally {
    renderer.unmount();
  }
});

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});
