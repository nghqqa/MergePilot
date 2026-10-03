// prod-copy-hygiene.test.mjs — PR-6 生产文案卫生回归锁。
// 断言用户可见渲染输出不含内部工单号/实现细节（C-\d+ / D-\d+ / SQLite / MU_MODE）；
// title tooltip 允许保留自托管技术事实（rag-live 端口 :4184、会话 Cookie 名 mp_session）。
// 复用 pages-smoke 基建：esbuild 打包真实 App + react-test-renderer + happy-dom，
// 走完整 <MemoryRouter><App/> 集成渲染；WorkspacePanel 单独直渲（顶栏弹层默认收起）。
// 运行：cd console/frontend && node --test test/prod-copy-hygiene.test.mjs
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

// 用户可见输出禁用模式：内部工单号与实现细节（环境变量名/存储引擎）。
const FORBIDDEN = /C-\d+|D-\d+|SQLite|MU_MODE/;

// —— DOM 环境（antd/cssinjs/rc-* 挂载副作用需要真实 DOM 实现）——
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

// —— fetch mock：按测试可重置的路由表 ——
const SESSION = { user: { name: 'smoke' }, repos: [] };
let ROUTES = {};
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

// bundle 构建一次：App + 配置缓存清理 + WorkspacePanel（顶栏弹层默认收起，需单独直渲）
let appMod = null;
async function loadApp() {
  if (appMod) return appMod;
  const outDir = path.join(FRONTEND, 'node_modules', '.copy-hygiene');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry,
    `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\n`
    + `import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};\n`
    + `import { WorkspacePanel } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/components/WorkspaceStatusPanel.jsx')))};\n`
    + 'export { App, clearRuntimeConfigCache, WorkspacePanel };\n');
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  const pick = (k) => mod[k] ?? mod.default?.[k] ?? null;
  appMod = { App: pick('App'), clear: pick('clearRuntimeConfigCache'), WorkspacePanel: pick('WorkspacePanel') };
  return appMod;
}

const LEGACY_HEALTH = () => [200, {
  service: 'console', data_mode: 'snapshot',
  sources: { primary: 'snapshot', snapshot: { available: true } },
}];
const COMMON = {
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, {
    service: 'console', data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } },
  }],
};

async function renderRoute(route) {
  const { App, clear } = await loadApp();
  if (clear) clear(); // config.js 模块级缓存跨用例隔离
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}

// 从 react-test-renderer JSON 树收集字符串：{ key: 属性名（children 内容为 'children'）, value }
function collectStrings(node, key = 'children', out = []) {
  if (node == null || typeof node !== 'object') return out;
  if (Array.isArray(node)) { node.forEach((n) => collectStrings(n, key, out)); return out; }
  if (typeof node === 'string') { out.push({ key, value: node }); return out; }
  if (typeof node === 'number') { out.push({ key, value: String(node) }); return out; }
  const props = node.props ?? null;
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (k === 'children') collectStrings(v, 'children', out);
      else if (typeof v === 'string') out.push({ key: k, value: v });
    }
  }
  if (node.children) collectStrings(node.children, 'children', out);
  return out;
}

// 断言：非 title 属性的用户可见字符串不命中禁用模式；返回 title 字符串数组
function assertClean(json, label) {
  const strings = collectStrings(JSON.parse(json));
  const offenders = strings.filter((s) => s.key !== 'title' && FORBIDDEN.test(s.value));
  assert.deepEqual(offenders, [], `${label}: 用户可见输出不得含内部工单号/实现细节，命中: `
    + offenders.map((o) => `[${o.key}] ${o.value}`).join(' | '));
  return strings.filter((s) => s.key === 'title').map((s) => s.value);
}

// 进程退出兜底（同 pages-smoke：bundle 依赖树存在模块级句柄）
after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

test('Approvals（legacy）：未接入文案不含工单号/SQLite，诚实语义保留', async () => {
  ROUTES = { '/api/auth/session': () => [200, SESSION], '/api/health': LEGACY_HEALTH() };
  const { renderer, json } = await renderRoute('/approvals');
  try {
    const text = json();
    assert.ok(text.includes('审批服务未接入'), '未接入语义必须保留');
    assert.ok(text.includes('仅提供审批票的存储与审计留痕'), '实现细节应改为人话（存储+审计留痕）');
    assert.ok(!text.includes('SQLite'), '不得出现 SQLite');
    assert.ok(text.includes('这里不会出现假的「批准」按钮'), '不伪装成功语义保留');
    assertClean(json(), 'Approvals legacy');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('Approvals（console-pg）：联调文案与 TTL 缺口说明不含工单号/表名', async () => {
  ROUTES = {
    ...COMMON,
    '/pg/api/approvals': () => [200, { items: [] }],
  };
  const { renderer, json } = await renderRoute('/approvals');
  try {
    const text = json();
    assert.ok(text.includes('张待审批票据'), '联调列表应渲染');
    assert.ok(text.includes('暂未携带有效期'), 'TTL 缺口说明应为人话');
    assert.ok(text.includes('当前没有待审批票据'), '诚实零值保留');
    assert.ok(!text.includes('approval.tickets'), '不得出现内部表名');
    assertClean(json(), 'Approvals console-pg');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('Knowledge：RAG 端口 :4184 只出现在 title tooltip，正文无工单号', async () => {
  ROUTES = { ...COMMON };
  const { renderer, json } = await renderRoute('/knowledge');
  try {
    const text = json();
    assert.ok(text.includes('RAG 检索'), '知识库卡片应渲染');
    const titles = assertClean(json(), 'Knowledge');
    assert.ok(titles.some((t) => t.includes(':4184')), '端口 :4184 应保留在 title tooltip');
    const contentStrings = collectStrings(JSON.parse(json())).filter((s) => s.key !== 'title').map((s) => s.value);
    assert.ok(!contentStrings.some((v) => v.includes(':4184')), '正文不得直出端口');
    assert.ok(!contentStrings.some((v) => v.includes('rag-live')), '正文不得直出内部服务名');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('Diagnostics：接线状态表不含工单号，等待语义保留', async () => {
  ROUTES = { ...COMMON };
  const { renderer, json } = await renderRoute('/diagnostics');
  try {
    const text = json();
    assert.ok(text.includes('接线状态'), '诊断页应渲染');
    assert.ok(text.includes('等待后端交付'), '未交付项的等待语义保留');
    assert.ok(text.includes('已接入'), '已接入项如实标注');
    assertClean(json(), 'Diagnostics');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('Settings（fixture）：DATA_MODE_COPY fixture 键人话文案，mp_session 只在 tooltip', async () => {
  ROUTES = { ...COMMON };
  const { renderer, json } = await renderRoute('/settings');
  try {
    const text = json();
    assert.ok(text.includes('隔离联调（fixture）'), 'fixture 模式应显示人话文案而非原始值');
    assert.ok(text.includes('GitHub OAuth 登录等待后端交付'), '登录方案文案不含工单号');
    const titles = assertClean(json(), 'Settings fixture');
    assert.ok(titles.some((t) => t.includes('mp_session')), 'Cookie 名应保留在 title tooltip');
    const contentStrings = collectStrings(JSON.parse(json())).filter((s) => s.key !== 'title').map((s) => s.value);
    assert.ok(!contentStrings.some((v) => v.includes('mp_session')), '正文不得直出 Cookie 名');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('Settings（MU）：多用户部署获专属数据模式文案，不冒用 legacy live 描述', async () => {
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, {
      service: 'console', data_mode: 'live',
      sources: { primary: 'multiuser', multiuser: { available: true, reason: 'mu_mode_active' } },
    }],
  };
  const { renderer, json } = await renderRoute('/settings');
  try {
    const text = json();
    assert.ok(text.includes('多用户实时数据'), 'MU 数据模式应显示专属文案');
    assert.ok(text.includes('租户实时收窄'), 'MU 语义=按会话租户收窄（与 /api/health 一致）');
    assert.ok(!text.includes('隔离 staging 库'), 'MU 不得冒用 legacy live 的 staging 库文案');
    assertClean(json(), 'Settings MU');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('MultiUser（未启用）：环境变量名改为人话，指向 BETA-GUIDE', async () => {
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': LEGACY_HEALTH(),
    '/api/mu/session': () => [200, { service_state: 'multiuser_not_enabled' }],
  };
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('多用户面未启用'), '未启用提示应渲染');
    assert.ok(text.includes('当前部署未启用多用户模式'), '人话事实说明');
    assert.ok(text.includes('BETA-GUIDE'), '启用指引指向公开文档');
    assertClean(json(), 'MultiUser notEnabled');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('MultiUser（OAuth 未配置）：登录步骤不含环境变量名与章节黑话', async () => {
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': LEGACY_HEALTH(),
    '/api/mu/session': () => [401, {}],
    '/api/mu/auth/providers': () => [200, { github: { configured: false } }],
  };
  const { renderer, json } = await renderRoute('/multiuser');
  try {
    const text = json();
    assert.ok(text.includes('GitHub OAuth 未配置'), '未配置提示应渲染');
    assert.ok(!text.includes('MU_GITHUB_OAUTH'), '不得直出环境变量名');
    assert.ok(!text.includes('§'), '不得出现文档章节黑话');
    assert.ok(text.includes('BETA-GUIDE'), '指引指向公开文档');
    assertClean(json(), 'MultiUser oauth-unconfigured');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('WorkspacePanel（console-pg）：接线状态不含工单号', async () => {
  const { WorkspacePanel } = await loadApp();
  const config = {
    mode: 'console-pg', dataMode: 'fixture',
    raw: { service: 'console', data_mode: 'fixture', sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } } },
  };
  const auth = { status: 'authed', user: { name: 'smoke' }, demo: false, refresh: () => {} };
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(WorkspacePanel, { config, auth, onRetry: () => {} }));
  });
  try {
    const json = () => JSON.stringify(renderer.toJSON());
    const text = json();
    assert.ok(text.includes('未交付——等待后端'), 'PR 聚合未交付文案');
    assert.ok(text.includes('隔离 test-auth（仅 fixture 票据）'), '审批决策（联调模式）文案');
    assertClean(json(), 'WorkspacePanel console-pg');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('WorkspacePanel（legacy contract 非 live）：未接线文案不含工单号', async () => {
  const { WorkspacePanel } = await loadApp();
  const config = {
    mode: 'contract', dataMode: 'snapshot',
    raw: { service: 'console', data_mode: 'snapshot', sources: { primary: 'snapshot', snapshot: { available: true } } },
  };
  const auth = { status: 'anonymous', user: null, demo: false, refresh: () => {} };
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(WorkspacePanel, { config, auth, onRetry: () => {} }));
  });
  try {
    const json = () => JSON.stringify(renderer.toJSON());
    assert.ok(json().includes('未接线——暂无审批票只读数据源'), '审批只读未接线文案');
    assert.ok(json().includes('未接线——决策接口与授权策略待后端交付'), '审批决策未接线文案');
    assertClean(json(), 'WorkspacePanel legacy');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('PrDetail（snapshot）：边界说明不含工单号，GitHub 权威语义保留', async () => {
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': LEGACY_HEALTH(),
    '/api/runs': () => [200, {
      data_mode: 'snapshot', items: [{
        run_id: 'run-a', pack_id: 'pack-a', repo: 'own/name', pr_number: 5, pr_title: 'sample pr',
        head_sha: 'a'.repeat(40), created_at: '2026-09-27 10:00:00',
        execution: { status: 'COMPLETED' }, review: { verdict: 'NOT_CONFIRMED' }, publish: { status: 'PUBLISHED' },
      }],
    }],
  };
  const { renderer, json } = await renderRoute('/repos/own/name/pr/5');
  try {
    const text = json();
    assert.ok(text.includes('以 GitHub 为准'), '当前 head 权威语义保留');
    assert.ok(text.includes('站内审批未接入'), '未接入语义保留');
    assertClean(json(), 'PrDetail snapshot');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('RepoPrs（snapshot）：快照口径说明不含工单号', async () => {
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': LEGACY_HEALTH(),
    '/api/runs': () => [200, {
      data_mode: 'snapshot', items: [{
        run_id: 'run-a', pack_id: 'pack-a', repo: 'own/name', pr_number: 5,
        head_sha: 'a'.repeat(40), created_at: '2026-09-27 10:00:00',
        execution: { status: 'COMPLETED' }, review: {}, publish: { status: 'PUBLISHED' },
      }],
    }],
  };
  const { renderer, json } = await renderRoute('/repos/own/name');
  try {
    const text = json();
    assert.ok(text.includes('历史数据中的仓库'), '快照口径说明应渲染');
    assert.ok(text.includes('不代表当前 head 状态'), '诚实口径保留');
    assertClean(json(), 'RepoPrs snapshot');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});
