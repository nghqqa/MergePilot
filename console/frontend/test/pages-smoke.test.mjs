// pages-smoke.test.mjs — P2 页面渲染冒烟：PrDetail（console-pg 路径）/ C 链 / RAG / Core（FXV 区块）。
// 复用 core-page 冒烟基建（esbuild 打包真实 App + react-test-renderer + happy-dom），
// 走完整 <MemoryRouter><App/> 集成渲染：mock /api/auth/session、/api/health 与各数据端点，
// 断言 status-map 中文标签真实渲染、错误态不出现。运行：cd console/frontend && node --test test/pages-smoke.test.mjs
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
let calls = [];
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  calls.push(u.pathname + u.search);
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

let appMod = null; // { App, clear }——bundle 构建一次，App 与 clearRuntimeConfigCache 同源取出
async function loadApp() {
  if (appMod) return appMod;
  const outDir = path.join(FRONTEND, 'node_modules', '.pages-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\n`
    + `import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};\n`
    + 'export { App, clearRuntimeConfigCache };\n');
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  // CJS bundle 互操作：named export 可能落在 mod.X 或 mod.default.X
  const pick = (k) => mod[k] ?? mod.default?.[k] ?? null;
  appMod = { App: pick('App'), clear: pick('clearRuntimeConfigCache') };
  return appMod;
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
  // config.js 模块级 cached 跨测试泄漏（前一个用例的 /api/health 声明会固化模式）——
  // 每次渲染前用测试辅助清缓存，保证本用例 ROUTES 的 health 声明真实生效
  const { App, clear } = await loadApp();
  if (clear) clear();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  // 多级异步链（session → health → config → data source → 页面查询）需要足够的微任务冲刷
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}

// 进程退出兜底：bundle 内联的依赖树（如 plots）存在模块级循环句柄，会阻止 node --test 子进程
// 退出；测试结果在 after 时已全部上报，此处显式退出仅为测试基建收尾（不影响结果判定）。
after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

test('PrDetail（console-pg）：SUCCEEDED/RUNNING/outcome 走 status-map 中文标签', async () => {
  ROUTES = {
    ...COMMON,
    '/pg/api/prs': (u) => [200, { items: [{ pr_number: 7, run_count: 2, latest_activity: '2026-09-27 10:00:00' }] }],
    '/pg/api/runs': (u) => [200, { items: [
      { run_id: 'run-a', pr_number: 7, status: 'SUCCEEDED', outcome: 'REVIEW_COMPLETED_ACTION_REQUIRED',
        head_sha: 'a'.repeat(40), created_at: '2026-09-27 10:00:00', mode: 'AUTO', run_class: 'review' },
      { run_id: 'run-b', pr_number: 7, status: 'RUNNING', outcome: null,
        head_sha: 'b'.repeat(40), created_at: '2026-09-27 10:05:00', mode: 'AUTO', run_class: 'review' },
    ] }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/repos/own/name/pr/7');
  try {
    const text = json();
    assert.ok(text.includes('运行历史'), 'PrDetail 应渲染运行历史区');
    assert.ok(text.includes('隔离联调'), '数据模式 chip 应含权威词"隔离联调"');
    assert.ok(text.includes('成功'), 'SUCCEEDED 应映射为中文标签"成功"');
    assert.ok(text.includes('处理中'), 'RUNNING 应映射为中文标签"处理中"');
    assert.ok(text.includes('审查完成 · 需人工处理'), 'REVIEW_COMPLETED_ACTION_REQUIRED 应映射为中文标签');
    assert.ok(!text.includes('请求失败') && !text.includes('is not defined'), '不得进入错误态/裸 JS 错误');
    assert.ok(calls.some((c) => c.startsWith('/pg/api/')), '应请求 /pg/api/*（console-pg 数据源真实生效）');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

// 回归锁（2026-10-02 用户报告第二段）：MU 部署下 legacy 路由 /repos/:owner/:name/pr/:n
// 曾漏分派 multiuser 源 → 掉进 SnapshotPrDetail（"历史快照中没有…运行记录"空页）。
// 修复：multiuser kind → 渲染 MuPrDetail（/api/mu/prs/:n 数据）。
test('PrDetail（multiuser）：legacy 路由分派 MuPrDetail，不再掉进历史快照空页', async () => {
  ROUTES = {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, {
      service: 'console', data_mode: 'live',
      sources: { primary: 'multiuser', multiuser: { available: true } },
    }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'repo-1', owner: 'nghqqa', name: 'test-repo', pr_count: 1, binding_state: 'active' } ] }],
    '/api/mu/prs/8': () => [200, {
      pull_request: { pr_id: 'pr-1', provider_pr_number: 8, title: 'fix: sample', head_sha: 'a1b2c3d4e5f6', state: 'open', repo_id: 'repo-1' },
      review_records: [], latest_run: null, my_permissions: { actions: [] } }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/repos/nghqqa/test-repo/pr/8');
  try {
    const text = json();
    assert.ok(!text.includes('历史快照中没有'), 'MU 模式不得掉进 snapshot 空页（回归锁：漏分派）');
    assert.ok(calls.some((c) => c.startsWith('/api/mu/prs/8')), '应请求 /api/mu/prs/8（MU 详情数据源生效）');
    assert.ok(text.includes('#8') || text.includes('PR #8'), '应显示 PR #8 标识');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('C 链页：组件态/总体态走 cchainMap 中文标签（NOT_CONFIGURED/MISSING/BLOCKED）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/cchain/status': () => [200, {
      overall: 'BLOCKED',
      enforce: { flag: true },
      blocked_conditions: ['MERGEPILOT_MODEL_CACHE_DIR 未设置', 'keystore 目录不存在'],
      components: [
        { component: 'model_cache', state: 'NOT_CONFIGURED', blocked_condition: 'MERGEPILOT_MODEL_CACHE_DIR 未设置' },
        { component: 'provider', state: 'ATTESTED', provider: 'stub' },
        { component: 'run_binding', state: 'MISSING', blocked_condition: 'keystore 目录不存在' },
      ],
    }],
    '/api/cchain/metrics': () => [200, { counters: { run_binding_verify_ok: 1 }, gauges: {} }],
  };
  const { renderer, json } = await renderRoute('/cchain');
  try {
    const text = json();
    assert.ok(text.includes('签名验证状态'), '签名验证页应渲染（术语纯化后）');
    assert.ok(text.includes('已阻断'), "overall BLOCKED 应映射'已阻断'");
    assert.ok(text.includes('未配置'), "NOT_CONFIGURED 应映射'未配置'");
    assert.ok(text.includes('已公证'), "ATTESTED 应映射'已公证'");
    assert.ok(text.includes('缺失'), "MISSING 应映射'缺失'");
    assert.ok(!text.includes('不可用'), '不得进入错误态');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('RAG 试验页：backend_not_wired 状态走 ragMap 中文标签', async () => {
  ROUTES = {
    ...COMMON,
    '/api/rag-trial/status': () => [200, { service_state: 'backend_not_wired', note: 'RAGTRIAL_PG_DSN 未配置——如实显示' }],
    '/api/rag-trial/metrics': () => [200, { totals: {} }],
  };
  const { renderer, json } = await renderRoute('/rag-trial');
  try {
    const text = json();
    assert.ok(text.includes('知识检索（试用）'), '知识检索页应渲染（术语纯化后）');
    assert.ok(text.includes('后端未接线'), "backend_not_wired 应映射'后端未接线'");
    assert.ok(text.includes('不构成 finding/ticket/gate/VERIFIED'), '边界文案应含 VERIFIED');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('Core 页 FXV 区块：ERROR_FATAL/VERIFIED 走 fxvMap 中文标签', async () => {
  ROUTES = {
    ...COMMON,
    '/api/pulls': () => [200, { source: 'BACKEND_NOT_WIRED', pulls: [] }],
    '/api/pending': () => [200, { source: 'BACKEND_NOT_WIRED', pending: [] }],
    '/api/tickets': () => [200, { source: 'BACKEND_NOT_WIRED', tickets: [] }],
    '/api/evidence': () => [200, { source: 'BACKEND_NOT_WIRED', evidence: [] }],
    '/api/audit': () => [200, { core_source: 'BACKEND_NOT_WIRED', audit_table: 'missing', gate_decisions: [] }],
    '/api/fxv/attempts': () => [200, {
      source: 'POSTGRESQL_LIVE',
      attempts: [
        { attempt_id: 'att-1', repo: 'a/b', branch: 'main', state: 'ERROR_FATAL', last_reason: 'step:patch', artifact_status: 'FAILED', audit_events: 2 },
        { attempt_id: 'att-2', repo: 'a/b', branch: 'main', state: 'VERIFIED', last_reason: '', artifact_status: 'COMPLETE', audit_events: 5 },
      ],
    }],
    '/api/fxv/metrics': () => [200, { metrics: {}, counters: {} }],
  };
  const { renderer, json } = await renderRoute('/core');
  try {
    const text = json();
    assert.ok(text.includes('自动修复编排'), '自动修复区块应渲染（术语纯化后）');
    assert.ok(text.includes('致命错误'), "ERROR_FATAL 应映射'致命错误'");
    assert.ok(text.includes('已验证'), "VERIFIED 应映射'已验证'");
    assert.ok(text.includes('归档校验通过'), "artifact COMPLETE 应映射'归档校验通过'");
    assert.ok(!text.includes('请求失败'), '不得进入错误态');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});
