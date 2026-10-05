// knowledge-usage.smoke.test.mjs — rc.11 PR-B 知识库「用量」区冒烟（真实接入后状态机锁）。
// 锁四态 + 设计红线：
//   ready（summary/by-skill/by-period 三 API 数据上屏）、空数据（诚实空态）、
//   网络失败（错误+重试）、401（引导登录）、403（无权限——read_pull_request 缺失）；
//   两条固定声明恒显示（"当前未提供 token 计量"/"未配置价目表，不显示金额"）+
//   覆盖口径声明（统计仅覆盖 v2 管线运行——不可用≠零）。
// 运行：cd console/frontend && node --test test/knowledge-usage.smoke.test.mjs（需先 npm ci）
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';
import { Window } from 'happy-dom';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const toFwd = (p) => p.replace(/\\/g, '/');

// —— DOM 环境（antd/cssinjs 挂载副作用需要真实 DOM 实现）——
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

// —— fetch mock：按 pathname+status 的路由表（usage 三端点可独立指定）——
const SESSION = { user: { name: 'smoke', repos: [] } };
let usageRoutes = {}; // { '/api/mu/usage/summary': [status, body], ... }
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  if (u.pathname === '/api/auth/session') {
    return new Response(JSON.stringify(SESSION), { status: 200, headers: { 'content-type': 'application/json' } });
  }
  const route = usageRoutes[u.pathname];
  if (route) {
    const [status, body] = route;
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
  }
  return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
};

let pagePromise = null;
function loadPage() {
  pagePromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.usage-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, [
      `import KnowledgePage from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/pages/KnowledgePage.jsx')))};`,
      `import { MemoryRouter } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'node_modules/react-router-dom/dist/index.js')))};`,
      'export { KnowledgePage, MemoryRouter };',
    ].join('\n'));
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler'],
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
    const mod = await import(pathToFileURL(bundle).href);
    return mod.default ?? mod;
  })();
  return pagePromise;
}

async function renderKnowledge() {
  const { KnowledgePage, MemoryRouter } = await loadPage();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, { initialEntries: ['/knowledge'] },
        React.createElement(KnowledgePage)),
    );
  });
  for (let i = 0; i < 8; i++) await act(async () => { await Promise.resolve(); });
  return renderer;
}

const okSummary = (skillTotal = 7, ragTotal = 3) => ({
  window: '30d',
  filters: { skill_key: null, agent_role: null, status: null, repo_id: null, pr_id: null },
  skill: { total: skillTotal, succeeded: 4, failed: 1, cancelled: 1, timeout: 0,
    success_rate: skillTotal ? 0.5714 : null, latency_p50_ms: skillTotal ? 100 : null,
    latency_p95_ms: skillTotal ? 280 : null,
    last_called_at: skillTotal ? '2026-10-05T00:00:00.000Z' : null },
  rag: { total: ragTotal, succeeded: 2, failed: 1, result_count_sum: 8,
    latency_p50_ms: 20, latency_p95_ms: 56, last_called_at: '2026-10-05T00:00:00.000Z' },
  token_metering: { available: false, reason: 'no_token_source' },
  cost: { available: false, reason: 'no_price_table' },
});

test('ready：三 API 数据上屏——统计卡/按 Skill 表/按日表/固定声明/覆盖口径', async () => {
  usageRoutes = {
    '/api/mu/usage/summary': [200, okSummary()],
    '/api/mu/usage/by-skill': [200, { window: '30d', total_groups: 2, limit: 50, offset: 0,
      rows: [
        { skill_key: 'skill-alpha', total: 3, succeeded: 2, failed: 1, success_rate: 0.6667,
          latency_p50_ms: 200, latency_p95_ms: 290, last_called_at: '2026-10-05T00:00:00.000Z' },
        { skill_key: 'skill-beta', total: 3, succeeded: 1, failed: 0, success_rate: 0.3333,
          latency_p50_ms: 50, latency_p95_ms: 50, last_called_at: '2026-10-04T00:00:00.000Z' },
      ] }],
    '/api/mu/usage/by-period': [200, { window: '30d', rows: [
      { day: '2026-10-04', skill_total: 1, skill_succeeded: 0, rag_total: 1 },
      { day: '2026-10-05', skill_total: 6, skill_succeeded: 4, rag_total: 2 },
    ] }],
  };
  const renderer = await renderKnowledge();
  try {
    const json = JSON.stringify(renderer.toJSON());
    assert.ok(json.includes('只读统计已接入'), '用量区升级为已接入 chip');
    assert.ok(json.includes('Skill 调用总数') && json.includes('57.1%'), '总数与成功率上屏');
    assert.ok(json.includes('RAG 检索次数'), 'RAG 检索次数上屏');
    assert.ok(json.includes('skill-alpha') && json.includes('按 Skill 聚合'), '按 Skill 聚合表上屏');
    assert.ok(json.includes('p50 耗时') && json.includes('最近调用'), '表列人话表头');
    assert.ok(json.includes('按日分布') && json.includes('2026-10-04'), '按日分布表上屏');
    assert.ok(json.includes('当前未提供 token 计量'), '固定声明一：token 计量');
    assert.ok(json.includes('未配置价目表，不显示金额'), '固定声明二：价目表');
    assert.ok(json.includes('统计仅覆盖 v2 管线运行'), '覆盖口径声明（不可用≠零）');
    assert.ok(json.includes('aria-live'), 'aria-live 状态播报在位');
    const tree = renderer.toJSON();
    const flat = JSON.stringify(tree);
    assert.ok(!flat.includes('数据源未接入'), '旧"数据源未接入"诚实空态已被真实接入替换');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('空数据：零调用 → 诚实空态（不伪造数值）', async () => {
  usageRoutes = {
    '/api/mu/usage/summary': [200, okSummary(0, 0)],
    '/api/mu/usage/by-skill': [200, { window: '30d', total_groups: 0, limit: 50, offset: 0, rows: [] }],
    '/api/mu/usage/by-period': [200, { window: '30d', rows: [] }],
  };
  const renderer = await renderKnowledge();
  try {
    const json = JSON.stringify(renderer.toJSON());
    assert.ok(json.includes('当前时间窗内无调用记录'), '诚实空态上屏');
    assert.ok(json.includes('当前未提供 token 计量'), '固定声明在空态仍显示');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('网络失败：错误态 + 重试按钮', async () => {
  usageRoutes = {
    '/api/mu/usage/summary': [503, { error: { reason: 'mu_store_unavailable' } }],
    '/api/mu/usage/by-skill': [200, { rows: [] }],
    '/api/mu/usage/by-period': [200, { rows: [] }],
  };
  const renderer = await renderKnowledge();
  try {
    const json = JSON.stringify(renderer.toJSON());
    assert.ok(json.includes('用量统计加载失败'), '错误态上屏');
    assert.ok(json.includes('重试'), '重试按钮在位');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('401：引导登录；403：无权限文案（read_pull_request 缺失）', async () => {
  usageRoutes = {
    '/api/mu/usage/summary': [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/usage/by-skill': [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/usage/by-period': [401, { error: { reason: 'unauthorized' } }],
  };
  const r401 = await renderKnowledge();
  try {
    const json = JSON.stringify(r401.toJSON());
    assert.ok(json.includes('未登录或会话已过期'), '401 引导登录文案');
    assert.ok(json.includes('/multiuser'), '登录入口链接在位');
  } finally {
    await act(async () => { r401.unmount(); });
  }
  usageRoutes = {
    '/api/mu/usage/summary': [403, { error: { reason: 'action_not_granted' } }],
    '/api/mu/usage/by-skill': [403, { error: { reason: 'action_not_granted' } }],
    '/api/mu/usage/by-period': [403, { error: { reason: 'action_not_granted' } }],
  };
  const r403 = await renderKnowledge();
  try {
    const json = JSON.stringify(r403.toJSON());
    assert.ok(json.includes('无权限查看用量统计'), '403 无权限文案');
  } finally {
    await act(async () => { r403.unmount(); });
  }
});

// 进程退出兜底（bundle 依赖树存在模块级句柄）
import { after } from 'node:test';
after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});
