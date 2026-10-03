// rag-model-panel.smoke.test.mjs — RAG-model-install PR4/5 前端冒烟：
// 安装面板四态（UNINSTALLED/DOWNLOADING/READY+ACTIVE/错误）+只读角色+manifest 披露+确认语义。
// 复用 skill-page.smoke 基建（esbuild 真实 App + react-test-renderer + happy-dom）。
import test from 'node:test';
import assert from 'node:assert/strict';
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
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.ragmodel-smoke');
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
  ...SESSION,
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, {
    service: 'console', data_mode: 'live',
    sources: { primary: 'multiuser', multiuser: { available: true } },
  }],
};
const ADMIN_SESSION = () => [200, {
  user: { user_id: 'u-1', login: 'admin1' },
  tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
  role: 'platform_admin', actions: ['read_repository', 'manage_instance'], memberships: [],
}];
const READONLY_SESSION = () => [200, {
  user: { user_id: 'u-2', login: 'viewer1' },
  tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
  role: 'maintainer', actions: ['read_repository'], memberships: [],
}];
const MANIFEST = () => [200, { manifest: {
  manifest_version: 'bge-m3-modelscope-v1', model_key: 'bge-m3', dims: 1024, license: 'MIT',
  source: { official_channel: 'modelscope', allowed_download_hosts: ['modelscope.cn'],
    download_url_template: 'https://modelscope.cn/api/v1/models/BAAI/bge-m3/repo?FilePath={path}&Revision=e44369c5623c',
    files_revision: 'e44369c5623cc146f016da906583db4ee0e3488d', repo_revision_short: 'a46a1381' },
  files: [
    { path: '1_Pooling/config.json', sha256: 'e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352', bytes: 191 },
    { path: 'pytorch_model.bin', sha256: 'b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38', bytes: 2271145830 },
  ],
  total_bytes: 2288244816,
} }];
const INSTALL = (state, extra = {}) => [200, { install: {
  model_key: 'bge-m3', state, active_provider: state === 'ACTIVE' ? 'bge-m3' : 'local-hash-v1',
  manifest_version: 'bge-m3-modelscope-v1', revision: 'e44369c5623cc146f016da906583db4ee0e3488d',
  license: 'MIT', expected_files: [
    { path: '1_Pooling/config.json', sha256: 'e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352', bytes: 191 },
    { path: 'pytorch_model.bin', sha256: 'b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38', bytes: 2271145830 },
  ], total_bytes: 2288244816, downloaded_bytes: 0, engine_busy: false,
  ...extra,
} }];

async function renderRoute(route) {
  const loaded = await loadApp();
  const App = loaded.App;
  if (loaded.clear) loaded.clear();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 80)); });
  const json = () => JSON.stringify(renderer.toJSON());
  return { json, unmount: () => renderer.unmount() };
}

test('安装面板：UNINSTALLED 态管理员可见安装按钮+manifest 披露+哈希声明', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': ADMIN_SESSION,
    '/api/mu/rag-model/manifest': MANIFEST,
    '/api/mu/rag-model/install': () => INSTALL('UNINSTALLED'),
  };
  const { json } = await renderRoute('/knowledge');
  const text = json();
  try {
    assert.ok(text.includes('RAG 模型安装'), '面板标题');
    assert.ok(text.includes('ModelScope 官方通道'), '官方通道声明');
    assert.ok(text.includes('哈希不匹配将拒绝激活'), '哈希不匹配声明');
    assert.ok(text.includes('安装（官方下载）'), '安装按钮');
    assert.ok(text.includes('local-hash'), '基线披露');
    assert.ok(text.includes('MIT'), '许可证');
    assert.ok(text.includes('e44369c5623c'), 'revision');
    assert.ok(text.includes('b5e0ce3470abf5ef3831'), '期望 sha256 披露');
    assert.ok(text.includes('未安装'), 'UNINSTALLED 人话');
    assert.ok(text.includes('始终可用'), 'local-hash 基线');
  } catch (e) { console.log(text.slice(0, 2500)); throw e; }
});

test('安装面板：READY 态=校验+激活（确认语义）；ACTIVE=回退；DOWNOADING=取消+进度', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': ADMIN_SESSION,
    '/api/mu/rag-model/manifest': MANIFEST,
    '/api/mu/rag-model/install': () => INSTALL('READY'),
  };
  const ready = (await renderRoute('/knowledge')).json();
  assert.ok(ready.includes('重新校验文件') && ready.includes('激活 bge-m3'), 'READY 操作');
  assert.ok(ready.includes('就绪'), 'READY 人话');
  ROUTES['/api/mu/rag-model/install'] = () => INSTALL('ACTIVE');
  const active = (await renderRoute('/knowledge')).json();
  assert.ok(active.includes('已激活') && active.includes('回退到 local-hash'), 'ACTIVE 操作');
  ROUTES['/api/mu/rag-model/install'] = () => INSTALL('DOWNLOADING', { downloaded_bytes: 1140624000, engine_busy: true });
  const dl = (await renderRoute('/knowledge')).json();
  assert.ok(dl.includes('取消下载') && dl.includes('下载中'), 'DOWNLOADING 操作+人话');
  ROUTES['/api/mu/rag-model/install'] = () => INSTALL('HASH_MISMATCH', { last_error_code: 'sha256_mismatch:pytorch_model.bin' });
  const hm = (await renderRoute('/knowledge')).json();
  assert.ok(hm.includes('哈希不匹配') && hm.includes('最近错误'), '失败态如实');
});

test('安装面板：只读角色无写按钮；错误态诚实', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': READONLY_SESSION,
    '/api/mu/rag-model/manifest': MANIFEST,
    '/api/mu/rag-model/install': () => INSTALL('READY'),
  };
  const ro = (await renderRoute('/knowledge')).json();
  assert.ok(ro.includes('当前角色只读'), '只读提示');
  assert.ok(!ro.includes('激活 bge-m3') && !ro.includes('重新校验文件'), '写按钮零出现');
  ROUTES['/api/mu/rag-model/install'] = () => [401, { error: { reason: 'unauthorized' } }];
  ROUTES['/api/mu/rag-model/manifest'] = () => [401, { error: { reason: 'unauthorized' } }];
  ROUTES['/api/mu/session'] = () => [401, { error: { reason: 'unauthorized' } }];
  const err = (await renderRoute('/knowledge')).json();
  assert.ok(err.includes('安装面板不可用') && err.includes('不回退演示数据'), '错误态诚实');
});
