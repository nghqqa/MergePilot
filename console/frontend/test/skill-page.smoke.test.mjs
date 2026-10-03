// skill-page.smoke.test.mjs — B 波技能治理页冒烟：
// /skills 真实 API 接线（列表/只读角色/诚实空态/错误态/区分声明/无 fixture 演练数据）。
// 复用 mu-review-arch.smoke 基建（esbuild 打包真实 App + react-test-renderer + happy-dom）。
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
  const outDir = path.join(FRONTEND, 'node_modules', '.skill-smoke');
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

async function renderRoute(route) {
  const loaded = await loadApp();
  const App = loaded.App;
  if (loaded.clear) loaded.clear();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 60)); });
  const json = () => JSON.stringify(renderer.toJSON());
  return { json, unmount: () => renderer.unmount() };
}

const SKILLS = [
  { skill_id: 's-1', skill_key: 'rag.retrieve', display_name: '审查检索技能', description: '检索',
    current_version: '1.1.0', state: 'active', updated_at: '2026-10-03T10:00:00Z', version_count: 2 },
  { skill_id: 's-2', skill_key: 'code.scan', display_name: '代码扫描', description: '扫描',
    current_version: null, state: 'disabled', updated_at: '2026-10-03T09:00:00Z', version_count: 0 },
];

test('技能页：管理员看到列表+发布/历史入口+区分声明', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'admin1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'platform_admin', actions: ['read_repository', 'manage_instance'], memberships: [],
    }],
    '/api/mu/skills': () => [200, { skills: SKILLS }],
  };
  const { json } = await renderRoute('/skills');
  const text = json();
  try {
    assert.ok(text.includes('技能版本治理'), '治理面声明标题');
    assert.ok(text.includes('"/skills"'), '导航含 /skills 入口（B 波补遗回归锁——#290 曾漏导航项）');
    assert.ok(text.includes('一经发布不可变'), '不可变声明（页面文案）');
    assert.ok(text.includes('rag.retrieve') && text.includes('审查检索技能'), '技能行（key+显示名）');
    assert.ok(text.includes('1.1.0') && text.includes('未发布'), '当前版本列（生效+未发布两态）');
    assert.ok(text.includes('启用') && text.includes('停用'), '状态列两态');
    assert.ok(text.includes('注册技能') && text.includes('发布新版本') && text.includes('版本历史'), '管理入口');
    assert.ok(text.includes('与「知识库 / 知识检索（试用）」相互独立'), '与 RAG 试用区分声明');
    assert.ok(text.includes('调用统计与留痕不在本页'), 'C 波边界声明');
    assert.ok(!text.includes('演练'), '零 fixture 演练字样');
  } catch (e) { console.log(text.slice(0, 2500)); throw e; }
});

test('技能页：maintainer 只读（无注册/发布/停用入口）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-2', login: 'm1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ['read_repository', 'decide_review'], memberships: [],
    }],
    '/api/mu/skills': () => [200, { skills: SKILLS }],
  };
  const { json } = await renderRoute('/skills');
  const text = json();
  try {
    assert.ok(text.includes('管理需平台管理员身份'), '只读提示');
    assert.ok(text.includes('版本历史'), '只读仍可看历史');
    assert.ok(!text.includes('注册技能') && !text.includes('发布新版本'), '写入口（注册/发布）零出现');
    assert.ok(!text.includes('停用技能') && !text.includes('启用技能'), '停用/启用确认框零出现（状态列标签除外）');
  } catch (e) { console.log(text.slice(0, 2500)); throw e; }
});

test('技能页：诚实空态与错误态（不回退演示数据）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'admin1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'platform_admin', actions: ['read_repository', 'manage_instance'], memberships: [],
    }],
    '/api/mu/skills': () => [200, { skills: [] }],
  };
  const empty = (await renderRoute('/skills')).json();
  assert.ok(empty.includes('暂无注册技能') && empty.includes('注册技能'), '空态+管理员引导');

  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/skills': () => [401, { error: { reason: 'unauthorized' } }],
  };
  const denied = (await renderRoute('/skills')).json();
  assert.ok(denied.includes('技能列表不可用') && denied.includes('不回退演示数据'), '未登录诚实错误态');
});
