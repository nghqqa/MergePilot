// mu-page.smoke.test.mjs — 多用户页（Developer Edition）渲染冒烟。
// 复用 pages-smoke 基建（esbuild 打包真实 App + react-test-renderer + happy-dom）：
// mock /api/mu/* 端点，锁"页面不因加载异常进入错误态 + 权限态/成员/绑定真实上屏 +
// 未启用时诚实提示"。运行：cd console/frontend && node --test test/mu-page.smoke.test.mjs
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
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-page-smoke');
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
  '/api/health': () => [200, {
    service: 'console',
    data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } },
  }],
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

test('多用户页：用户/tenant/角色/动作面/成员/绑定上屏（后端会话数据真实渲染）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'dana' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Migration Default Tenant', is_migration_tenant: true },
      role: 'maintainer',
      actions: ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query', 'request_review', 'decide_review', 'request_repair', 'manage_repository_binding'],
      memberships: [],
    }],
    '/api/mu/members': () => [200, { members: [
      { membership_id: 'm-1', login: 'dana', role: 'maintainer', state: 'active', created_at: '2026-09-27T10:00:00Z' },
      { membership_id: 'm-2', login: 'gina', role: 'auditor', state: 'active', created_at: '2026-09-27T10:05:00Z' },
    ] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001',
        binding_id: 'b-1', binding_kind: 'fixture', installation_state: 'active', granted_scopes: ['pull_requests:read'] },
    ] }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('dana'), '当前用户 login 上屏');
    assert.ok(text.includes('default'), 'tenant slug 上屏');
    assert.ok(text.includes('maintainer'), '角色上屏');
    assert.ok(text.includes('request_repair'), '动作面（服务端判定）上屏');
    assert.ok(text.includes('gina') && text.includes('auditor'), '成员只读视图渲染');
    assert.ok(text.includes('acme') || text.includes('app'), '仓库数据在页面中体现');
    // Phase onboarding：权限快照在 GitHub App 面板内（绑定状态标签）而非独立列
    assert.ok(text.includes('acme') && text.includes('app'), '仓库信息上屏');
    assert.ok(text.includes('按钮仅反映权限'), '权限提示语上屏（授权以后端为准）');
    assert.ok(!text.includes('请求失败') && !text.includes('is not defined'), '不得进入错误态/裸 JS 错误');
    assert.ok(calls.some((c) => c.startsWith('/api/mu/')), '应请求 /api/mu/* 真实端点');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('多用户页：MU_MODE=legacy 时诚实提示未启用（零推断数据）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, { service_state: 'multiuser_not_enabled',
      note: 'MU_MODE != multiuser 或 CONSOLE_PG_DSN 未配置——多用户面未启用（如实不伪装）' }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('多用户面未启用'), '未启用提示上屏');
    assert.ok(text.includes('multiuser'), '启用条件（MU_MODE=multiuser）提示上屏');
    assert.ok(!calls.some((c) => c.startsWith('/api/mu/members')), '未启用时不拉成员数据');
    assert.ok(!text.includes('请求失败'), '未启用不是错误态');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});
