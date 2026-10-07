// mu-pr-detail-copy-close.smoke.test.mjs — rc.17 实测两修复的回归锁（2026-10-07）。
// 覆盖：
//   1) 执行器文案：内置规则引擎不得再宣称「开发/测试路径/非生产」；档位只来自本 run
//      的策略快照字段（review_run.llm_mode，GET /api/mu/runs/:id 的 run.*），缺失时不猜测
//      不编造（中性文案收尾）。
//   2) 关闭交互无回归：关闭钮 → 占位态回归 + 焦点回列表行；Escape 文档级关闭同样生效。
// 复用 mu-detail-actions 基建：esbuild 打包真实 App + fetch 路由 mock + react-dom/happy-dom
// 真实 DOM 交互（react-test-renderer 无法承载 antd/真实点击）。
// 运行：cd console/frontend && node --test test/mu-pr-detail-copy-close.smoke.test.mjs（需先 npm ci）
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
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
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const SESSION = { user: { name: 'smoke' }, repos: [] };
let ROUTES = {};
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u, init);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-copy-close-smoke');
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

const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };
// 轮询等待（详情二次取数 runs 列表→run 详情需要多于单轮 flush 的 act 循环）
const waitText = async (ui, probe, rounds = 60) => {
  for (let i = 0; i < rounds; i++) {
    if (ui.text().includes(probe)) return true;
    await act(async () => { await Promise.resolve(); });
  }
  return ui.text().includes(probe);
};

// run 详情夹具：withLlm=false 时省略 llm_mode（缺失字段→中性文案，不猜测）
const RUN_DETAIL = (withLlm) => [200, {
  run: {
    run_id: 'run-1', status: 'WAITING_FOR_HUMAN_APPROVAL', provider_pr_number: 11,
    head_sha: 'ab'.repeat(20), policy_version: 1,
    ...(withLlm ? { llm_mode: 'deterministic_only' } : {}),
  },
  attempts: [{ agent_role: 'reviewer', attempt: 1, status: 'DONE', provider: 'deterministic',
    latency_ms: 3100, created_at: '2026-10-07T13:45:46Z', error_code: null,
    input_digest: 'i'.repeat(8), output_digest: 'o'.repeat(8) }],
  findings: [], fixes: [], verifications: [], decisions: [], dead_letters: [],
}];

const BASE_ROUTES = (detail) => ({
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, { service: 'console', data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } } }],
  '/api/mu/session': () => [200, {
    user: { user_id: 'u-1', login: 'dana' },
    tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
    role: 'maintainer',
    actions: ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query',
      'request_review', 'decide_review', 'request_repair', 'manage_repository_binding'],
    memberships: [],
  }],
  '/api/mu/github/app/status': () => [200, { configured: true }],
  '/api/mu/github/installations': () => [200, { installations: [
    { installation_id: 1, revoked: null, suspended: null }] }],
  '/api/mu/github/installations/1/repositories': () => [200, { repositories: [] }],
  '/api/mu/repositories': () => [200, { repositories: [
    { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001',
      binding_id: 'b-1', binding_kind: 'github_app', installation_state: 'active' }] }],
  '/api/mu/invitations': () => [200, { invitations: [] }],
  '/api/mu/members': () => [200, { members: [] }],
  '/api/mu/prs': () => [200, { pull_requests: [
    { pr_id: 'pr-1', provider_pr_number: 11, head_sha: 'ab'.repeat(20),
      branch_protection_status: 'unknown', state: 'open',
      updated_at: '2026-10-07T13:45:49Z' }] }],
  // 详情内容块（MuPrDetailContent）按编号双寻址：/api/mu/prs/:prNumber
  '/api/mu/prs/11': () => [200, {
    pull_request: { pr_id: 'pr-1', provider_pr_number: 11, head_sha: 'ab'.repeat(20),
      branch_protection_status: 'unknown', title: 'rc.17 验收', state: 'open' },
    latest_run: null,
    review_records: [],
    my_permissions: { actions: ['read_repository', 'read_pull_request'] },
  }],
  '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: [] }],
  '/api/mu/runs': () => [200, { runs: [{ run_id: 'run-1', provider_pr_number: 11 }] }],
  '/api/mu/runs/run-1': () => detail,
  // 管理面板路由（agent-policy/agentteams-status/review-policy/providers/egress-events）
  // 刻意不 mock——404 走页面既有"加载失败"降级（与生产探针口径一致，避免臆造形状破坏渲染）
});

async function renderMultiUser() {
  const App = await loadApp();
  const container = domWindow.document.createElement('div');
  domWindow.document.body.appendChild(container);
  let root = null;
  await act(async () => {
    root = createRoot(container);
    root.render(React.createElement(MemoryRouter, { initialEntries: ['/multiuser'] }, React.createElement(App)));
  });
  await flush();
  return {
    text: () => container.textContent ?? '',
    clickPrRow: async () => {
      const row = [...container.querySelectorAll('.mu-pr-row')].find((x) => x.textContent.includes('#11'));
      assert.ok(row, 'PR #11 行已渲染');
      await act(async () => { row.click(); });
      await flush();
    },
    clickClose: async () => {
      const btn = container.querySelector('.mu-detail-close');
      assert.ok(btn, '关闭详情按钮存在');
      await act(async () => { btn.click(); });
      await flush();
    },
    pressEscape: async () => {
      await act(async () => {
        domWindow.document.dispatchEvent(new domWindow.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      });
      await flush();
    },
    cleanup: async () => {
      if (root) await act(async () => root.unmount());
      container.remove();
    },
  };
}

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

// ── 1. 有 llm_mode：档位来自 run 快照，绝不宣称开发/测试路径 ──
test('内置审查文案：显示档位快照，不再宣称开发/测试/非生产', async () => {
  ROUTES = BASE_ROUTES(RUN_DETAIL(true));
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await waitText(ui, '执行器：内置规则引擎'), '管线面板就绪');
    const text = ui.text();
    assert.ok(text.includes('内置规则引擎（确定性审查）'), '审查引擎中性新文案上屏');
    assert.ok(text.includes('本 run 审查档位快照 deterministic_only'), '档位=run 策略快照字段原值');
    assert.ok(!text.includes('开发/测试路径'), '不再宣称开发/测试路径');
    assert.ok(!text.includes('非生产'), '不再宣称非生产');
  } finally { await ui.cleanup(); }
});

// ── 2. 缺 llm_mode：不猜测不编造，中性收尾 ──
test('缺失策略字段：中性文案，无档位编造', async () => {
  ROUTES = BASE_ROUTES(RUN_DETAIL(false));
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await waitText(ui, '执行器：内置规则引擎'), '管线面板就绪');
    const text = ui.text();
    assert.ok(text.includes('内置规则引擎（确定性审查）'), '中性引擎文案仍在');
    assert.ok(!text.includes('档位快照'), '缺失字段不显示档位子句');
    assert.ok(!text.includes('evidence_only'), '不用租户当前配置冒充 run 策略');
    assert.ok(!text.includes('开发/测试路径'), '缺失时同样不得宣称开发/测试路径');
  } finally { await ui.cleanup(); }
});

// ── 3. 关闭交互无回归：按钮关闭+焦点恢复；Escape 文档级关闭 ──
test('关闭回归：按钮关闭回占位+焦点回列表行；Escape 同样关闭', async () => {
  ROUTES = BASE_ROUTES(RUN_DETAIL(true));
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(ui.text().includes('审查管线'), '详情已展开');
    await ui.clickClose();
    assert.ok(ui.text().includes('选择一个 PR 查看详情'), '关闭后回占位态');
    const focusedRow = domWindow.document.activeElement;
    assert.ok(focusedRow && focusedRow.classList?.contains('mu-pr-row')
      && focusedRow.textContent.includes('#11'), '焦点恢复到 #11 列表行');
    // 重新打开后用 Escape 关闭（文档级监听回归）
    await ui.clickPrRow();
    assert.ok(ui.text().includes('审查管线'), '详情再次展开');
    await ui.pressEscape();
    assert.ok(ui.text().includes('选择一个 PR 查看详情'), 'Escape 关闭详情');
  } finally { await ui.cleanup(); }
});
