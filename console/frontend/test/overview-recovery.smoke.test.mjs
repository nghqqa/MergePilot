// overview-recovery.smoke.test.mjs — 概览页错误恢复行为回归（2026-10-16 修复包）。
// 锁三件事：
//  ① 首载失败 → 点击重试成功 → 退出错误态并显示列表（修复前 error 残留导致
//     RecoveryBox 常驻——拿到数据也回不到列表）；
//  ② 已有数据时刷新失败 → 旧数据保留 + 「刷新失败」时点提示出现；
//  ③ 后续刷新成功 → 刷新错误提示消失、数据仍在。
// 刷新间隔（30s）用 node:test mock timers 推进，不等待真实时间。
// 运行：cd console/frontend && node --test test/overview-recovery.smoke.test.mjs（需先 npm ci）
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const toFwd = (p) => p.replace(/\\/g, '/');

// —— DOM 环境（antd Table/rc-resize-observer 需要，与 overview-link 冒烟同款）——
import { Window } from 'happy-dom';
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

let overviewRoute = () => [500, { error: { reason: 'backend_error' } }];
const SESSION = { user: { name: 'smoke', login_type: 'github-oauth' } };
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  if (u.pathname === '/api/auth/session') {
    return new Response(JSON.stringify(SESSION), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  if (u.pathname === '/api/overview') {
    const [status, body] = overviewRoute();
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  }
  return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
};

let pagesPromise = null;
function loadPages() {
  pagesPromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.ov-recovery-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const shim = path.join(outDir, 'plots-shim.mjs');
    fs.writeFileSync(shim, 'export const Line = () => null;\nexport const Column = () => null;\nexport const Bar = () => null;\n');
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, [
      `import OverviewPage from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/pages/OverviewPage.jsx')))};`,
      `import { AuthProvider } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/auth.jsx')))};`,
      `import { MemoryRouter } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'node_modules/react-router-dom/dist/index.js')))};`,
      'export { OverviewPage, AuthProvider, MemoryRouter };',
    ].join('\n'));
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler'],
      alias: { '@ant-design/plots': shim },
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
    const mod = await import(pathToFileURL(bundle).href);
    return mod.default ?? mod;
  })();
  return pagesPromise;
}

const okOverview = (prs) => ({
  source: 'MU_CANONICAL_LIVE', data_source: 'MU_CANONICAL_LIVE',
  prs, repository_counts: [], trend: [], stage_counts: {},
  pending_summary: { count: 0, oldest_pending_at: null, oldest_wait_minutes: null },
  incidents: { stale_count: 0, failed_receipts: 0, integrity_conflicts: 0 },
  health: { postgres: 'LIVE', minio: { state: 'AGENTTEAMS_MANAGED', note: '' } },
  mode: 'mu_canonical',
});
const flush = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await Promise.resolve(); }); };
const label = (n) => String(Array.isArray(n.props?.children) ? n.props.children.join('') : (n.props?.children ?? ''));

test('首载失败→重试成功→列表出现；刷新失败→旧数据保留+时点提示；刷新恢复→提示消失', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const { OverviewPage, AuthProvider, MemoryRouter } = await loadPages();
  overviewRoute = () => [500, { error: { reason: 'backend_error' } }];
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(AuthProvider, null,
        React.createElement(MemoryRouter, { initialEntries: ['/overview'] },
          React.createElement(OverviewPage))),
    );
  });
  await flush();
  try {
    // ① 首载失败：整页错误态（RecoveryBox role=alert + 重试按钮），无 PR 行
    let text = JSON.stringify(renderer.toJSON());
    assert.ok(text.includes('重试'), '错误态应有重试按钮');
    assert.ok(text.includes('PR 号缺失') === false && !text.includes('/repos/smoke/'), '错误态不渲染 PR 行');
    // ② 切换为成功数据 → 点重试 → 列表出现、错误态退出
    overviewRoute = () => [200, okOverview([
      { repo: 'smoke/long-repository-name-for-recovery-check', pr: 8, head_sha: 'a1b2c3d4e5f6',
        run_id: 'run-1', stage: 'BLOCKED', stage_source: 'mu_review_run', updated_at: '2026-10-16T10:00:00Z' },
    ])];
    const retry = renderer.root.find((n) => typeof n.props?.onClick === 'function' && label(n).includes('重试'));
    await act(async () => { retry.props.onClick(); });
    await flush();
    text = JSON.stringify(renderer.toJSON());
    assert.ok(text.includes('/repos/smoke/long-repository-name-for-recovery-check/pr/8'), '重试成功后 PR 行出现');
    assert.ok(!text.includes('role":"alert') || !/工作台数据/.test(text), '错误态应退出');
    // ③ 已有数据时刷新失败：mock 推进 30s 间隔 → 旧数据保留 + 刷新失败横幅
    overviewRoute = () => [500, { error: { reason: 'network_error' } }];
    await act(async () => { t.mock.timers.tick(30_000); });
    await flush();
    text = JSON.stringify(renderer.toJSON());
    assert.ok(text.includes('/repos/smoke/long-repository-name-for-recovery-check/pr/8'), '刷新失败时旧数据保留');
    assert.ok(text.includes('刷新失败'), '刷新失败横幅应出现');
    assert.ok(text.includes('时点的快照'), '应如实标注数据时点');
    // ④ 后续刷新成功：提示消失、数据仍在
    overviewRoute = () => [200, okOverview([
      { repo: 'smoke/long-repository-name-for-recovery-check', pr: 8, head_sha: 'a1b2c3d4e5f6',
        run_id: 'run-1', stage: 'REVIEWING', stage_source: 'mu_review_run', updated_at: '2026-10-16T11:00:00Z' },
    ])];
    await act(async () => { t.mock.timers.tick(30_000); });
    await flush();
    text = JSON.stringify(renderer.toJSON());
    assert.ok(!text.includes('刷新失败——当前显示上次成功数据'), '刷新恢复后提示消失');
    assert.ok(text.includes('/repos/smoke/long-repository-name-for-recovery-check/pr/8'), '数据仍在');
  } finally {
    await act(async () => { renderer.unmount(); });
    t.mock.timers.reset();
  }
});
