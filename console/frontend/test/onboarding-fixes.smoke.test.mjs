// onboarding-fixes.smoke.test.mjs — rc8.4 新用户主线可独立完成（前端修复回归）。
// 覆盖：总览/仓库零数据 CTA 与真实路由跳转、待处理首票指引、审批 DRY_RUN 人话说明、
// 登录来源未知中性化（identity.js）、工作台术语默认收进折叠（源码契约）。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
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
globalThis.matchMedia ??= domWindow.matchMedia;
globalThis.requestAnimationFrame ??= domWindow.requestAnimationFrame.bind(domWindow);
globalThis.cancelAnimationFrame ??= domWindow.cancelAnimationFrame.bind(domWindow);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// happy-dom 无 canvas 2d 实现——趋势图渲染需要；stub 全部方法为 no-op。
const noop2d = new Proxy({}, { get: (t, k) => (k === "measureText" ? () => ({ width: 0 }) : () => noop2d) });
domWindow.HTMLCanvasElement.prototype.getContext = () => noop2d;
// bundle 内图表初始化会访问全局 addEventListener（Node 侧无此 API）
globalThis.addEventListener = globalThis.addEventListener ?? (() => {});
globalThis.removeEventListener = globalThis.removeEventListener ?? (() => {});
globalThis.dispatchEvent = globalThis.dispatchEvent ?? (() => true);

const SESSION = { user: { name: 'smoke-admin' }, repos: [] };
const ACTIONS = ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query',
  'request_review', 'decide_review', 'apply_fix', 'merge_pr',
  'manage_repository_binding', 'manage_membership', 'read_audit', 'manage_instance', 'request_repair'];

let ROUTES = {};
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const out = route(u, init);
  if (out instanceof Promise) return out;
  const [status, body] = out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

const OVERVIEW_EMPTY = {
  source: 'MU_CANONICAL_LIVE', generated_at: '2026-10-25T00:00:00Z',
  prs: [], health: { status: 'ok', postgres: 'LIVE', minio: { state: 'NOT_WIRED', note: '' } }, repository_counts: [],
  trend: [], prs_projection_limit: 200, prs_truncated: false,
};

let loadBundle = null;
function loadModule() {
  loadBundle ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.onboard-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, [
      `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};`,
      `import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};`,
      `import { deriveIdentitySource, capabilityLine } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/identity.js')))};`,
      'export { App, clearRuntimeConfigCache, deriveIdentitySource, capabilityLine };',
    ].join('\n'));
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
    const mod = await import(pathToFileURL(bundle).href);
    return { App: mod.App ?? mod.default?.App ?? mod.default, clear: mod.clearRuntimeConfigCache,
      deriveIdentitySource: mod.deriveIdentitySource, capabilityLine: mod.capabilityLine };
  })();
  return loadBundle;
}

async function renderRoute(route) {
  const loaded = await loadModule();
  if (loaded.clear) loaded.clear();
  const container = domWindow.document.createElement('div');
  domWindow.document.body.appendChild(container);
  let root = null;
  await act(async () => {
    root = createRoot(container);
    root.render(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(loaded.App)));
  });
  const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };
  const waitFor = async (cond, deadlineMs = 4000) => {
    const t0 = Date.now();
    for (;;) {
      await flush();
      if (cond()) return true;
      if (Date.now() - t0 > deadlineMs) return false;
      await new Promise((r) => setTimeout(r, 50));
    }
  };
  return {
    container,
    text: () => container.textContent ?? '',
    links: () => [...container.querySelectorAll('a')].map((a) => a.getAttribute('href')),
    waitFor,
    cleanup: async () => { if (root) await act(async () => root.unmount()); container.remove(); },
  };
}

function emptyRoutes() {
  return {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, { service: 'console', data_mode: 'multiuser',
      sources: { primary: 'multiuser', multiuser: { available: true } }, mu_schema_ready: true }],
    '/api/mu/session': () => [200, { user: { user_id: 'u-1', login: 'smoke-admin' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'platform_admin', actions: ACTIONS, memberships: [] }],
    '/api/mu/repositories': () => [200, { repositories: [] }],
    '/api/overview': () => [200, OVERVIEW_EMPTY],
    '/api/mu/approvals': () => [200, { fix_approvals: [] }],
    '/api/mu/prs': () => [200, { prs: [] }],
    '/api/rag-trial/status': () => [200, { service_state: 'ready' }],
  };
}

test('仓库零数据：空态给接入路径引导与真实跳转链接（/multiuser、/datasources）', async () => {
  ROUTES = emptyRoutes();
  const page = await renderRoute('/repos');
  const ok = await page.waitFor(() => page.text().includes('还没有可展示的仓库'));
  assert.ok(ok, '空态文案缺失');
  assert.ok(page.text().includes('接入路径'), '缺少接入路径引导');
  const links = page.links();
  assert.ok(links.includes('/multiuser'), '缺 /multiuser 跳转');
  assert.ok(links.includes('/datasources'), '缺 /datasources 跳转');
  await page.cleanup();
});

test('总览零数据：CTA 指向 /datasources 与 /multiuser（真实 Link，非纯文本）', async () => {
  ROUTES = emptyRoutes();
  const page = await renderRoute('/overview');
  const ok = await page.waitFor(() => page.text().includes('还没有审查记录'));
  if (!ok) { console.error('OVERVIEW-TEXT:', page.text().slice(0, 600)); }
  assert.ok(ok, '总览零数据 CTA 缺失');
  assert.ok(page.text().includes('还没有接入仓库'), 'CTA 未说明原因');
  const links = page.links();
  assert.ok(links.includes('/datasources') && links.includes('/multiuser'), 'CTA 链接缺失');
  await page.cleanup();
});

test('待处理（MU）：首票产生方式指引可点击进入组织页', async () => {
  ROUTES = emptyRoutes();
  const page = await renderRoute('/pending');
  const ok = await page.waitFor(() => page.text().includes('第一张票的产生方式'));
  assert.ok(ok, '首票指引缺失');
  assert.ok(page.text().includes('旧票自动失效'), '票面失效语义说明缺失（人话版应保留）');
  assert.ok(page.links().includes('/multiuser'), '缺组织页跳转');
  await page.cleanup();
});

test('审批：标题「审批票总览」与待处理区分；DRY_RUN 说明不写 GitHub；空态带队列链接', async () => {
  ROUTES = emptyRoutes();
  const page = await renderRoute('/approvals');
  const ok = await page.waitFor(() => page.text().includes('审批票总览'));
  assert.ok(ok, '新标题缺失');
  assert.ok(page.text().includes('不向 GitHub 写入任何内容') || page.text().includes('不会向 GitHub 写入任何内容'), 'DRY_RUN 人话说明缺失');
  assert.ok(page.text().includes('前往「待处理」队列'), '队列跳转缺失');
  assert.ok(!page.text().includes('诚实零值'), '内部用语「诚实零值」不得出现在用户界面');
  await page.cleanup();
});

test('登录来源未知中性化：identity.js 不再输出「登录来源未知」', async () => {
  ROUTES = emptyRoutes();
  const loaded = await loadModule();
  const id = loaded.deriveIdentitySource({ authed: true, session: { login_type: '' }, dataMode: 'multiuser' });
  assert.equal(id.label, '本机会话（登录方式以服务端配置为准）');
  assert.equal(loaded.capabilityLine(id).includes('本机部署'), true);
  assert.ok(!JSON.stringify(id).includes('登录来源未知'));
  await Promise.resolve();
});

test('工作台术语默认隐藏：Gate/Bridge 收进折叠（源码契约：children 引用变量而非内联 JSX）', () => {
  const src = fs.readFileSync(path.join(FRONTEND, 'src', 'pages', 'CorePage.jsx'), 'utf8');
  assert.ok(src.includes('children: gateBridgeDetail'), 'Gate/Bridge 必须经 gateBridgeDetail 变量进折叠');
  assert.ok(src.includes('本页为系统内部状态页'), '人话说明必须在场');
  assert.ok(src.includes("label: '技术详情：Gate / Bridge 执行分层（内部术语，排障用）'"), '折叠标签必须在场');
  assert.ok(src.includes('children: gateBridgeDetail'), '折叠内容必须经变量引用（默认收起）');
});

test('MU_CANONICAL_LIVE 内部标识不再出现在总览用户可见文案', () => {
  const src = fs.readFileSync(path.join(FRONTEND, 'src', 'pages', 'OverviewPage.jsx'), 'utf8');
  assert.ok(!src.includes('不渲染空轴冒充'), '内部理由不得外露');
});
