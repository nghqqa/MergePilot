// overview-link.smoke.test.mjs — 运营总览「PR 阶段明细」链接回归锁（2026-10-02 用户报告）。
// 根因：MU 部署 /api/overview 的 prs 投影字段为 pr（mu-console-api），而 OverviewPage
// 链接读 r.pr_number → 生成 /repos/.../pr/undefined 死链（点进旧快照页即空态）。
// 本测试在 node 内渲染真实 OverviewPage（MemoryRouter + plots shim）：
//   - MU 形状（pr 字段）→ 链接 /pr/8 且无 /pr/undefined
//   - legacy 形状（pr_number 字段）→ 链接照常
//   - 缺值行 → 纯文本（PR 号缺失），绝不渲染 /pr/undefined
// 运行：cd console/frontend && node --test test/overview-link.smoke.test.mjs（需先 npm ci）
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

// —— DOM 环境（与 core-page 冒烟同款：antd/cssinjs 需要）——
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

// —— fetch mock ——
const SESSION = { user: { name: 'smoke', repos: [] } };
let overviewBody = null;
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  if (u.pathname === '/api/auth/session') {
    return new Response(JSON.stringify(SESSION), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  if (u.pathname === '/api/overview' && overviewBody) {
    return new Response(JSON.stringify(overviewBody), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
};

const muOverview = (prs, trend = null) => ({
  source: 'MU_CANONICAL_LIVE', data_source: 'MU_CANONICAL_LIVE',
  prs, repository_counts: [], trend: trend ?? [], stage_counts: {},
  pending_summary: { count: 0, oldest_pending_at: null, oldest_wait_minutes: null },
  incidents: { stale_count: 0, failed_receipts: 0, integrity_conflicts: 0 },
  // health 形状对齐 mu-console-api overview 投影（minio.state 等被页面直接读取）
  health: { postgres: 'LIVE',
    minio: { state: 'AGENTTEAMS_MANAGED', note: 'MinIO 由 AgentTeams 内部管理' },
    backend: { state: 'OK', note: '本服务即后端（只读）' } },
  mode: 'mu_canonical',
});

// —— 打包：OverviewPage + AuthProvider + MemoryRouter（@ant-design/plots 用 shim 替换，
//    图表渲染与被测链接无关；react/react-dom external 保证单实例）——
let pagesPromise = null;
function loadPages() {
  pagesPromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.ov-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const shim = path.join(outDir, 'plots-shim.mjs');
    fs.writeFileSync(shim, 'export const Line = () => null;\nexport const Column = () => null;\n');
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, [
      `import OverviewPage from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/pages/OverviewPage.jsx')))};`,
      `import { AuthProvider } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/auth.jsx')))};`,
      `import { MemoryRouter } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'node_modules/react-router-dom/dist/index.js')))};`,
      'export { OverviewPage, AuthProvider, MemoryRouter };',
    ].join('\n'));
    await build({
      entryPoints: [entry],
      bundle: true,
      format: 'cjs',
      platform: 'node',
      outfile: bundle,
      jsx: 'automatic',
      external: ['react', 'react-dom', 'scheduler'],
      alias: { '@ant-design/plots': shim },
      define: { 'process.env.NODE_ENV': '"test"' },
      logLevel: 'silent',
    });
    const mod = await import(pathToFileURL(bundle).href);
    return mod.default ?? mod;
  })();
  return pagesPromise;
}

async function renderOverview(prs, trend = undefined) {
  const { OverviewPage, AuthProvider, MemoryRouter } = await loadPages();
  overviewBody = muOverview(prs, trend);
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(AuthProvider, null,
        React.createElement(MemoryRouter, { initialEntries: ['/overview'] },
          React.createElement(OverviewPage))),
    );
  });
  for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve(); });
  return renderer;
}

test('MU 形状（pr 字段）：链接指向 /pr/8，绝不生成 /pr/undefined', async () => {
  const renderer = await renderOverview([
    { repo: 'nghqqa/test-repo', pr: 8, head_sha: 'a1b2c3d4e5f6', run_id: 'run-1',
      stage: 'REMEDIATING', stage_source: 'mu_review_run', latest: null },
  ]);
  try {
    const json = JSON.stringify(renderer.toJSON());
    assert.ok(json.includes('/repos/nghqqa/test-repo/pr/8'), '应生成带真实 PR 号的链接');
    assert.ok(json.includes('#8'), '标签应显示 #8');
    assert.ok(!json.includes('/pr/undefined'), '不得出现 /pr/undefined 死链');
    assert.ok(!json.includes('undefined'), '渲染树不得残留 undefined 文本');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('legacy 形状（pr_number 字段）链接照常；缺值行渲染纯文本不出链接', async () => {
  const renderer = await renderOverview([
    { repo: 'nghqqa/legacy-repo', pr_number: 12, head_sha: 'b1b2c3d4e5f6', run_id: 'run-2',
      stage: 'REVIEWING', stage_source: 'mu_review_run', latest: null },
    { repo: 'nghqqa/orphan-repo', head_sha: 'c1b2c3d4e5f6', run_id: 'run-3',
      stage: 'UNKNOWN', stage_source: 'mu_review_run', latest: null },
  ]);
  try {
    const json = JSON.stringify(renderer.toJSON());
    assert.ok(json.includes('/repos/nghqqa/legacy-repo/pr/12'), 'legacy pr_number 行链接照常');
    assert.ok(json.includes('PR 号缺失'), '缺值行应显示 PR 号缺失纯文本');
    assert.ok(!json.includes('/pr/undefined'), '缺值行不得生成 /pr/undefined 链接');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('趋势区诚实空态：trend=[] 显示文案不渲染空轴；有数据时不显示空态', async () => {
  // 空数组（桩值/无数据）→ 文案，绝不画空图冒充
  const emptyRenderer = await renderOverview([], []);
  try {
    const emptyJson = JSON.stringify(emptyRenderer.toJSON());
    assert.ok(emptyJson.includes('没有趋势数据'), 'trend=[] 应显示诚实空态文案');
    assert.ok(emptyJson.includes('不渲染空轴冒充'), '空态文案应说明不冒充');
  } finally {
    await act(async () => { emptyRenderer.unmount(); });
  }
  // 14 天数据（generate_series 桩）→ 不显示空态文案（图表本体由 plots shim 替换，不在此断言）
  const day = (i, runs) => ({ date: `2026-09-${String(i + 1).padStart(2, '0')}`, runs });
  const dataRenderer = await renderOverview([], Array.from({ length: 14 }, (_, i) => day(i, i)));
  try {
    const dataJson = JSON.stringify(dataRenderer.toJSON());
    assert.ok(!dataJson.includes('没有趋势数据'), 'trend 有数据时不得显示空态');
  } finally {
    await act(async () => { dataRenderer.unmount(); });
  }
});
