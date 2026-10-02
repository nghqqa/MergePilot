// core-page.smoke.test.mjs — /core 页面加载冒烟测试（P1 修复波新增）。
// 背景：CorePage 曾因解构遗漏引用未声明变量 `fxv` 抛 ReferenceError，整页进入"请求失败"
// 错误态（此前无任何测试覆盖 /core 加载路径）。本测试在 node 内渲染真实组件：
//   esbuild 打包 CorePage.jsx（含依赖链）→ react-test-renderer 渲染（无浏览器）
//   → mock fetch 提供 7 个端点的诚实空值（BACKEND_NOT_WIRED）
//   → 断言：主内容渲染、全部端点被请求、页面未进入错误态。
// 运行：cd console/frontend && node --test test/core-page.smoke.test.mjs（需先 npm ci）
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(__dirname, '..');
const toFwd = (p) => p.replace(/\\/g, '/');

// —— DOM 环境：happy-dom 提供真实 DOM 实现（antd/cssinjs/rc-* 挂载副作用需要）——
import { Window } from 'happy-dom';
const domWindow = new Window();
if (!globalThis.window) globalThis.window = domWindow;   // canUseDom() 需要 window 存在，缺失时 cssinjs injectCSS 返回 null
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

// —— fetch mock：/core 依赖的端点（诚实空值；POST logout 不在加载路径）——
const SESSION = { user: { name: 'smoke' }, repos: [] };
const ROUTES = {
  '/api/auth/session': () => [200, SESSION],
  '/api/pulls': () => [200, { source: 'BACKEND_NOT_WIRED', pulls: [] }],
  '/api/pending': () => [200, { source: 'BACKEND_NOT_WIRED', pending: [] }],
  '/api/tickets': () => [200, { source: 'BACKEND_NOT_WIRED', tickets: [] }],
  '/api/evidence': () => [200, { source: 'BACKEND_NOT_WIRED', evidence: [] }],
  '/api/audit': () => [200, { core_source: 'BACKEND_NOT_WIRED', audit_table: 'missing', gate_decisions: [] }],
  '/api/fxv/attempts': () => [200, { source: 'BACKEND_NOT_WIRED', attempts: [] }],
  '/api/fxv/metrics': () => [200, { metrics: {}, counters: {} }],
};
const calls = [];
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  calls.push(u.pathname);
  const route = ROUTES[u.pathname];
  if (!route) {
    return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  }
  const [status, body] = route();
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

// 打包一个薄入口（AuthProvider 也在 JSX 文件中，须一并打包后才能在 node 导入）。
// react/react-dom 标记 external 且 bundle 写入 node_modules/.core-smoke/ 下——
// 保证与测试自身 import 的是同一个 React 实例（双实例会导致 hooks 报 null dispatcher）。
let pagesPromise = null;
function loadPages() {
  pagesPromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.core-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, [
      `import CorePage from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/pages/CorePage.jsx')))};`,
      `import { AuthProvider } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/auth.jsx')))};`,
      'export { CorePage, AuthProvider };',
    ].join('\n'));
    await build({
      entryPoints: [entry],
      bundle: true,
      format: 'cjs',
      platform: 'node',
      outfile: bundle,
      jsx: 'automatic',
      external: ['react', 'react-dom', 'scheduler'],
      define: { 'process.env.NODE_ENV': '"test"' },
      logLevel: 'silent',
    });
    // CJS bundle 经 import() 拿到的是 { default: module.exports }（antd/lib 为 CJS，须 cjs 输出）
    const mod = await import(pathToFileURL(bundle).href);
    return mod.default ?? mod;
  })();
  return pagesPromise;
}

test('/core 页面加载冒烟：渲染主内容、请求全部端点、不进错误态', async () => {
  const { CorePage, AuthProvider } = await loadPages();

  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(AuthProvider, null, React.createElement(CorePage)),
    );
  });
  // 冲刷异步加载链（session → authed → load() 的 Promise.all）
  for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve(); });

  try {
    const json = JSON.stringify(renderer.toJSON());
    assert.ok(json.includes('系统状态与接线'), '主内容应渲染（页面标题）');
    assert.ok(json.includes('Gate / Bridge 分层'), '数据区块应渲染（data 加载成功后才有）');
    assert.ok(!json.includes('请求失败'), '/core 不得进入错误态（回归锁：fxv ReferenceError）');
    assert.ok(!json.includes('is not defined'), '不得向用户暴露裸 JS 错误');
    for (const p of Object.keys(ROUTES)) {
      assert.ok(calls.includes(p), `端点应被请求：${p}`);
    }
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

// —— 回归锁（2026-10-02 用户报告）：MU 部署下 /core 误报"请检查 PG 连接"、
// PR 列全 '—'。根因：Wave 3.7 五个 Core API 在 MU 模式返回 source=MU_CANONICAL_LIVE
// 与 pr/run 形状（pr 非 pr_number），CorePage 只认 POSTGRESQL_LIVE + pr_number。
test('/core MU 模式：健康接线显示成功标签，不误报 PG 错误；pr/run/审计事件诚实展示', async () => {
  const MU_PULL = { repo: 'nghqqa/test-repo', pr: 8, title: 'fix: sample', head_sha: 'a1b2c3d4e5f6',
    run_id: 'run-abc123', status: 'FIX_QUEUED', stage: 'FIX_QUEUED' };
  Object.assign(ROUTES, {
    '/api/pulls': () => [200, { source: 'MU_CANONICAL_LIVE', data_source: 'MU_CANONICAL_LIVE', pulls: [MU_PULL] }],
    '/api/pending': () => [200, { source: 'MU_CANONICAL_LIVE', pending: [
      { run_id: 'run-abc123', status: 'REWORK_REQUIRED', repo: 'nghqqa/test-repo', pr: 8, title: 'fix: sample', updated_at: '2026-10-02T00:00:00Z' } ] }],
    '/api/tickets': () => [200, { tickets: [], capability: 'mu_tickets_managed_in_multiuser_page' }],
    '/api/evidence': () => [200, { evidence: [], capability: 'mu_evidence_managed_in_multiuser_page' }],
    '/api/audit': () => [200, { source: 'MU_CANONICAL_LIVE', audit: [
      { kind: 'review_policy.updated', detail: { actor: 'nghqqa' }, created_at: '2026-10-02T00:00:00Z' } ] }],
    '/api/fxv/attempts': () => [200, { capability: 'fxv_persistence_not_tenant_scoped', attempts: [] }],
  });

  const { CorePage, AuthProvider } = await loadPages();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(AuthProvider, null, React.createElement(CorePage)),
    );
  });
  for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve(); });

  try {
    const json = JSON.stringify(renderer.toJSON());
    assert.ok(!json.includes('请检查 PG 连接'), 'MU_CANONICAL_LIVE 是健康接线，不得渲染 PG 连接错误');
    assert.ok(!json.includes('请求失败'), 'MU 模式不得进入请求失败错误态');
    assert.ok(json.includes('MU 实时数据'), '应显示 MU 实时数据成功标签');
    assert.ok(json.includes('#8'), 'PR 列应显示 #8（读 pr 字段），不得为 —');
    assert.ok(json.includes('run-abc123'), 'Run 列应显示 run_id');
    assert.ok(json.includes('需返工'), '待处理状态应映射 MU run 状态（REWORK_REQUIRED→需返工）');
    assert.ok(json.includes('审计事件') && json.includes('review_policy.updated'),
      'MU audit 数组应渲染为审计事件表（kind 投影）');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});
