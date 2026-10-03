// run-trace.smoke.test.mjs — C 波 C2：运行详情"Skill/RAG 调用留痕"冒烟。
// 锁定 MU live 域契约（/api/mu/runs/:runId/skill-invocations | rag-retrievals | call-summary）：
//   1) 200 有数据：摘要计数 + 表格行字段 + 三段语义边界文案逐字上屏；
//   2) 真实空集；3) legacy not_available（不伪造 v2 数据）；
//   4) 403 / 401 / 404 错误分形；5) 网络失败 → 错误块 + 重试（点重试重新 fetch）；
//   6) partial（RUNNING/INTERRUPTED → 提示条）；7) 未知 status / invocation_kind 原值兜底；
//   8) digest 截断（前 12 位）+ title 全文；9) 契约白名单外字段（prompt/正文）不渲染；
//   10) aria-live/role=status 反馈；11) 移动端 390 结构性无横向溢出（表格走 table-scroll
//       容器 + 截断样式，happy-dom 无布局引擎——按既有测试口径做 DOM 结构 + CSS 规则断言）；
//   12) 完整 App 路由挂载：/mu/repos/.../pr/:n 经审查管线面板可达本区域。
// 复用 mu-detail-actions 基建（esbuild 打包真实组件 + react-dom + happy-dom 真实 DOM）。
// 运行：cd console/frontend && node --test test/run-trace.smoke.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { after } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { Window } from 'happy-dom';
import { fmtTime } from '../src/format.js';

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

// ── 契约夹具（与 C1 逐字对齐；串入 prompt/响应/文档正文字段验证白名单渲染）──
const RUN_ID = 'run-1';
const SHA_A = 'a'.repeat(64);
const SHA_B = 'b'.repeat(64);
const SHA_C = 'c'.repeat(64);

const SKILL_ITEMS = [
  { event_id: 'ev-1', agent_role: 'reviewer', skill_key: 'org.sec-review', skill_version: 'v1.4.2',
    invocation_kind: 'agentteams_round', status: 'SUCCEEDED',
    started_at: '2026-10-01T08:30:00Z', completed_at: '2026-10-01T08:30:12Z',
    latency_ms: 12500, input_digest: SHA_A, output_digest: SHA_B, error_code: null,
    prompt: 'SHOULD_NOT_RENDER_PROMPT', raw_response: 'SHOULD_NOT_RENDER_RESPONSE' },
  { event_id: 'ev-2', agent_role: 'verifier', skill_key: 'org.test-runner', skill_version: 'v2.0.1',
    invocation_kind: 'verifier_tool', status: 'FAILED',
    started_at: '2026-10-01T08:31:00Z', completed_at: '2026-10-01T08:31:01Z',
    latency_ms: 850, input_digest: SHA_C, output_digest: null,
    error_code: 'E_TEST_RUNNER_TIMEOUT_CODE_9F2A' },
];
const RAG_ITEMS = [
  { event_id: 'rag-1', agent_role: 'reviewer', status: 'SUCCEEDED',
    started_at: '2026-10-01T08:30:05Z', completed_at: '2026-10-01T08:30:06Z',
    latency_ms: 420, query_digest: SHA_C, result_count: 5,
    source_digest_list: [SHA_A, SHA_B], error_code: null,
    query: 'SHOULD_NOT_RENDER_QUERY', documents: ['SHOULD_NOT_RENDER_DOC_BODY'] },
];
const SUMMARY = {
  skill: { total: 2, by_status: { SUCCEEDED: 1, FAILED: 1 }, by_role: { reviewer: 1, verifier: 1 } },
  rag: { total: 1, by_status: { SUCCEEDED: 1 } },
};
const RUN_LIST_ROW = { run_id: RUN_ID, provider_pr_number: 4242, status: 'COMPLETED',
  trigger_source: 'manual', head_sha: 'ab'.repeat(20) };
const RUN_DETAIL = { run: { run_id: RUN_ID, status: 'COMPLETED', head_sha: 'cd'.repeat(20),
  trigger_source: 'manual', architecture_version: 'v2' },
  attempts: [], findings: [], decisions: [], fixes: [], verifications: [], dead_letters: [] };

const okRun = (items, total, extra = {}) => [200, { run: { run_id: RUN_ID, legacy: false }, items, total, ...extra }];
const LEGACY_BODY = { run: { run_id: RUN_ID, legacy: true }, items: [], total: 0, not_available: true };

// ── fetch 路由 mock（按 pathname 精确匹配；route 值为 [status, body] 或返回它的函数）──
let ROUTES = {};
const fetchCalls = [];
globalThis.fetch = async (input) => {
  const url = typeof input === 'string' ? input : input.url;
  const u = new URL(url, 'http://smoke.local');
  fetchCalls.push(u.pathname);
  const route = ROUTES[u.pathname];
  if (!route) {
    return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }),
      { status: 404, headers: { 'content-type': 'application/json' } });
  }
  const out = await (typeof route === 'function' ? route(u) : route);
  const [status, body] = out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};
const countCalls = (p) => fetchCalls.filter((x) => x === p).length;

// 管线面板（RunTracePanel 的生产挂载点）直达夹具：/api/mu/runs → run 详情 → 留痕三端点
function traceRoutes({
  skill = okRun(SKILL_ITEMS, SKILL_ITEMS.length),
  rag = okRun(RAG_ITEMS, RAG_ITEMS.length),
  summary = [200, SUMMARY],
} = {}) {
  return {
    '/api/mu/runs': () => [200, { runs: [RUN_LIST_ROW] }],
    '/api/mu/runs/run-1': () => [200, RUN_DETAIL],
    '/api/mu/runs/run-1/skill-invocations': skill,
    '/api/mu/runs/run-1/rag-retrievals': rag,
    '/api/mu/runs/run-1/call-summary': summary,
  };
}

// ── esbuild 打包（两份 bundle：PipelinePanel 直达 / 完整 App 路由链）──
async function bundle(entrySource, tag) {
  const outDir = path.join(FRONTEND, 'node_modules', `.run-trace-smoke-${tag}`);
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundlePath = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry, entrySource);
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundlePath,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  return import(pathToFileURL(bundlePath).href);
}

async function loadPipelinePanel() {
  const mod = await bundle(
    `import { PipelinePanel } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/pages/MultiUserPage.jsx')))};\n`
    + `export { PipelinePanel };`,
    'panel');
  return mod.PipelinePanel;
}
async function loadApp() {
  const mod = await bundle(
    `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\n`
    + `import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};\n`
    + `export { App, clearRuntimeConfigCache };`,
    'app');
  return mod;
}

const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };

async function renderInto(container, element) {
  let root = null;
  await act(async () => {
    root = createRoot(container);
    root.render(element);
  });
  await flush();
  return {
    el: container,
    text: () => container.textContent ?? '',
    click: (label) => {
      const needle = label.replace(/\s+/g, '');
      const btn = [...container.querySelectorAll('button')]
        .find((b) => (b.textContent ?? '').replace(/\s+/g, '').includes(needle));
      assert.ok(btn, `可点击目标：${label}`);
      btn.click();
    },
    cleanup: async () => { if (root) await act(async () => root.unmount()); container.remove(); },
  };
}

async function renderPanel() {
  const PipelinePanel = await loadPipelinePanel();
  const container = domWindow.document.createElement('div');
  domWindow.document.body.appendChild(container);
  return renderInto(container, React.createElement(PipelinePanel, { prNumber: 4242, repoId: 'r-1' }));
}

const COMMON = {
  '/api/auth/session': () => [200, { user: { name: 'smoke' }, repos: [] }],
  '/api/health': () => [200, { service: 'console', data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } } }],
  '/api/mu/session': () => [200, { user: { user_id: 'u-1', login: 'smoke1' },
    tenant: { tenant_id: 't-1', slug: 'default' }, role: 'reviewer',
    actions: ['read_pull_request'], memberships: [] }],
  '/api/mu/repositories': () => [200, { repositories: [{ repo_id: 'r-1', owner: 'acme', name: 'app' }] }],
  '/api/mu/prs/4242': () => [200, {
    pull_request: { pr_id: 'pr-1', provider_pr_number: 4242, head_sha: 'ab'.repeat(20),
      branch_protection_status: 'known_clean', title: 'fix' },
    review_records: [],
    latest_run: { run_id: RUN_ID, status: 'COMPLETED', architecture_version: 'v2' },
    my_permissions: { actions: ['read_pull_request'] } }],
  '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: [] }],
};

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

const DISCLOSURES = [
  '仅显示调用元数据与摘要，不包含 prompt、响应、代码正文或 RAG 文档正文。',
  '调用记录不等于审查通过、测试通过或合并资格。',
  'Skill 已激活不代表本次运行一定调用了该 Skill。',
];

// ── 1. 200 有数据 ──
test('200 有数据：摘要计数 + 角色分组 + 版本 + 表格行字段 + 三段文案逐字上屏', async () => {
  fetchCalls.length = 0;
  ROUTES = traceRoutes();
  const page = await renderPanel();
  try {
    const t = page.text();
    // 摘要计数（服务端 total 口径）
    assert.ok(t.includes('Skill 调用次数'), 'Skill 调用次数标签上屏');
    assert.ok(t.includes('RAG 检索次数'), 'RAG 检索次数标签上屏');
    assert.ok(t.includes('按 Agent 角色分组'), '角色分组标签上屏');
    assert.ok(t.includes('Skill version'), '版本标签上屏');
    // 按 Agent 角色分组计数（reviewer/verifier；人话角色标签）
    assert.ok(t.includes('Reviewer（审查）×1'), 'reviewer 角色计数上屏');
    assert.ok(t.includes('Verifier（独立验证）×1'), 'verifier 角色计数上屏');
    assert.ok(t.includes('v1.4.2, v2.0.1'), '本 run 出现的 Skill version 汇总上屏');
    // 表格行字段
    assert.ok(t.includes('org.sec-review'), 'skill_key 上屏');
    assert.ok(t.includes('AgentTeams 轮次'), 'invocation_kind 人话标签上屏');
    assert.ok(t.includes('Verifier 工具调用'), 'verifier_tool 人话标签上屏');
    assert.ok(t.includes('成功'), 'SUCCEEDED 中文状态上屏');
    assert.ok(t.includes('失败'), 'FAILED 中文状态上屏');
    assert.ok(t.includes('12.5 秒'), 'latency 人话（12500ms）上屏');
    assert.ok(t.includes('850 ms'), 'latency 人话（850ms）上屏');
    assert.ok(t.includes(fmtTime('2026-10-01T08:30:00Z')), 'started_at fmtTime 上屏');
    assert.ok(t.includes('E_TEST_RUNNER_TIMEOUT_CODE_9F2A'), 'error code 上屏');
    // 三段明确文案（逐字）
    for (const d of DISCLOSURES) assert.ok(t.includes(d), `明确文案逐字上屏：${d.slice(0, 18)}…`);
    // 全终态无 partial 提示条
    assert.ok(!t.includes('部分调用仍在进行或被恢复补记'), '全终态不显示 partial 提示条');
  } finally { await page.cleanup(); }
});

// ── 2. 真实空集 ──
test('真实空集：Skill/RAG 各自诚实空文案，计数为 0，不冒充数据', async () => {
  ROUTES = traceRoutes({
    skill: okRun([], 0), rag: okRun([], 0),
    summary: [200, { skill: { total: 0, by_status: {}, by_role: {} }, rag: { total: 0, by_status: {} } }],
  });
  const page = await renderPanel();
  try {
    const t = page.text();
    assert.ok(t.includes('本次运行没有 Skill 调用记录（真实空集，非降级）'), 'Skill 真实空集文案');
    assert.ok(t.includes('本次运行没有 RAG 检索记录（真实空集，非降级）'), 'RAG 真实空集文案');
    assert.ok(!t.includes('org.sec-review'), '空集不渲染任何行数据');
    for (const d of DISCLOSURES) assert.ok(t.includes(d), '空集同样携带语义边界文案');
  } finally { await page.cleanup(); }
});

// ── 3. legacy not_available ──
test('legacy 运行：not_available=true → 明确"v2 之前不可用"文案，不伪造 v2 数据', async () => {
  ROUTES = traceRoutes({ skill: [200, LEGACY_BODY], rag: [200, LEGACY_BODY] });
  const page = await renderPanel();
  try {
    const t = page.text();
    assert.ok(t.includes('该运行产生于 v2 管线之前，调用留痕不可用'), 'legacy 文案上屏');
    assert.ok(!t.includes('org.sec-review'), 'legacy 不渲染任何伪造调用行');
    assert.ok(!t.includes('Skill 调用次数'), 'legacy 不显示伪造的计数面板');
  } finally { await page.cleanup(); }
});

// ── 4. 403 / 401 / 404 错误分形 ──
test('403 显示无权文案；401 登录失效；404 跨租户不可见——均不渲染半份数据', async () => {
  ROUTES = traceRoutes({ skill: [403, { error: { reason: 'forbidden' } }] });
  let page = await renderPanel();
  try {
    const t = page.text();
    assert.ok(t.includes('当前角色无权查看调用留痕（403）'), '403 文案上屏');
    assert.ok(!t.includes('org.sec-review'), '403 不渲染数据');
  } finally { await page.cleanup(); }

  ROUTES = traceRoutes({ rag: [401, { error: { reason: 'not_authenticated' } }] });
  page = await renderPanel();
  try {
    assert.ok(page.text().includes('登录状态已失效（401）'), '401 文案上屏');
  } finally { await page.cleanup(); }

  ROUTES = traceRoutes({ summary: [404, { error: { reason: 'run_not_found' } }] });
  page = await renderPanel();
  try {
    assert.ok(page.text().includes('该运行不存在或不在当前组织范围内（404）'), '404 文案上屏');
  } finally { await page.cleanup(); }
});

// ── 5. 网络失败 → 错误块 + 重试（点重试重新 fetch）──
test('网络失败：错误块 + 重试按钮；点重试重新 fetch 三端点并恢复数据', async () => {
  fetchCalls.length = 0;
  ROUTES = traceRoutes({ skill: () => { throw new TypeError('fetch failed'); } });
  const page = await renderPanel();
  try {
    const t = page.text();
    assert.ok(t.includes('调用留痕读取失败'), '错误块上屏');
    assert.ok(t.includes('网络错误'), '网络失败原因上屏');
    const skillPath = `/api/mu/runs/${RUN_ID}/skill-invocations`;
    const firstCount = countCalls(skillPath);
    assert.ok(firstCount >= 1, '失败前已发起 fetch');
    // 恢复路由 → 点重试 → 重新 fetch + 数据上屏
    ROUTES = traceRoutes();
    await act(async () => { page.click('重试'); });
    await flush();
    assert.ok(countCalls(skillPath) > firstCount, '点重试重新 fetch');
    assert.ok(page.text().includes('org.sec-review'), '重试成功后数据上屏');
    assert.ok(!page.text().includes('调用留痕读取失败'), '错误块撤下');
  } finally { await page.cleanup(); }
});

// ── 6. partial：RUNNING / INTERRUPTED → 提示条 ──
test('存在 RUNNING/INTERRUPTED 事件：提示条上屏，INTERRUPTED 状态徽章为"已恢复补记"', async () => {
  const partialSkill = [
    { ...SKILL_ITEMS[0], status: 'RUNNING', latency_ms: null, output_digest: null },
    { ...SKILL_ITEMS[1], status: 'INTERRUPTED' },
  ];
  ROUTES = traceRoutes({ skill: okRun(partialSkill, 2) });
  const page = await renderPanel();
  try {
    const t = page.text();
    assert.ok(t.includes('部分调用仍在进行或被恢复补记'), 'partial 提示条上屏');
    assert.ok(t.includes('进行中'), 'RUNNING 中文状态上屏');
    assert.ok(t.includes('已恢复补记'), 'INTERRUPTED 中文状态上屏');
  } finally { await page.cleanup(); }
});

// ── 7. 未知 status / invocation_kind 兜底 ──
test('未知 status 与 invocation_kind：原值兜底显示，title 标注未知状态，不崩溃', async () => {
  const weird = [{ ...SKILL_ITEMS[0], status: 'WEIRD_STATE', invocation_kind: 'future_kind_xyz' }];
  ROUTES = traceRoutes({ skill: okRun(weird, 1) });
  const page = await renderPanel();
  try {
    const t = page.text();
    assert.ok(t.includes('WEIRD_STATE'), '未知 status 原值显示（不吞机器值）');
    assert.ok(t.includes('future_kind_xyz'), '未知 invocation_kind 原值显示');
    const badge = [...page.el.querySelectorAll('.badge')].find((b) => b.textContent.includes('WEIRD_STATE'));
    assert.ok(badge, '未知状态徽章在 DOM');
    assert.ok((badge.getAttribute('title') ?? '').includes('未知状态'), 'badge title 含"未知状态"note');
  } finally { await page.cleanup(); }
});

// ── 8. digest 截断 + title 全文 ──
test('digest 前 12 位截断显示，title 携带全文；RAG 来源 digest 逐条同规则', async () => {
  ROUTES = traceRoutes();
  const page = await renderPanel();
  try {
    const digests = [...page.el.querySelectorAll('.trace-digest')];
    assert.ok(digests.length >= 4, '输入/输出/查询/来源摘要均有渲染');
    const FULL = new Set([SHA_A, SHA_B, SHA_C]);
    for (const d of digests) {
      assert.equal(d.textContent.length, 12, `digest 截断为 12 位（实际 ${d.textContent.length}）`);
      const full = d.getAttribute('title');
      assert.ok(FULL.has(full), 'title 为 digest 全文');
      assert.ok(full.startsWith(d.textContent), 'title 全文以截断前缀开头');
    }
    const byTitle = digests.filter((d) => d.getAttribute('title') === SHA_A);
    assert.ok(byTitle.length >= 2, '同一 digest 在输入摘要与来源摘要中重复出现且规则一致');
    assert.ok(page.text().includes('命中数'), 'RAG 命中数列上屏');
    assert.ok(page.text().includes('5'), 'result_count 命中数上屏');
  } finally { await page.cleanup(); }
});

// ── 9. 白名单：契约外字段（prompt/响应/查询/文档正文）不渲染 ──
test('响应串入 prompt/raw_response/query/documents 等契约外字段：一律不上屏', async () => {
  ROUTES = traceRoutes();
  const page = await renderPanel();
  try {
    const t = page.text();
    for (const leak of ['SHOULD_NOT_RENDER_PROMPT', 'SHOULD_NOT_RENDER_RESPONSE',
      'SHOULD_NOT_RENDER_QUERY', 'SHOULD_NOT_RENDER_DOC_BODY']) {
      assert.ok(!t.includes(leak), `白名单外字段不渲染：${leak}`);
    }
  } finally { await page.cleanup(); }
});

// ── 10. loading + aria-live 反馈 ──
test('loading 态：骨架 + 加载中文案；aria-live/role=status 反馈节点存在并随状态更新', async () => {
  let releaseSkill = null;
  ROUTES = traceRoutes({
    skill: () => new Promise((resolve) => { releaseSkill = resolve; }),
  });
  const page = await renderPanel();
  try {
    assert.ok(page.text().includes('正在读取调用留痕'), '加载中文案上屏');
    assert.ok(page.el.querySelector('[aria-label="调用留痕加载中"]'), 'loading 容器 role/status 上屏');
    const live = page.el.querySelector('[aria-live="polite"][role="status"]');
    assert.ok(live, 'aria-live polite + role=status 节点存在');
    assert.ok(live.textContent.includes('正在读取'), '加载中 aria 反馈内容');
    await act(async () => {
      releaseSkill([200, { run: { run_id: RUN_ID, legacy: false }, items: SKILL_ITEMS, total: SKILL_ITEMS.length }]);
    });
    await flush();
    assert.ok(live.textContent.includes('已加载'), '加载完成后 aria 反馈更新为已加载');
    assert.ok(page.text().includes('org.sec-review'), '数据上屏');
  } finally { await page.cleanup(); }
});

// ── 11. 移动端 390：结构性无页面级横向溢出 ──
// happy-dom 无布局引擎（scrollWidth 恒 0），按既有测试口径断言：
//   a) 渲染后视口切到 390 不抛错；b) 每张留痕表都包在 .table-scroll（overflow-x:auto，横向滚动
//   限制在表格容器内）；c) 长文本单元格走 .trace-ellipsis（max-width+ellipsis 截断）；
//   d) CSS：.trace-table 无 min-width（不撑破父容器）、窄屏档有更小截断宽度。
test('移动端 390：表格全部走 table-scroll 容器 + 截断样式，CSS 无页面级溢出来源', async () => {
  ROUTES = traceRoutes();
  try { domWindow.happyDOM?.setViewport?.({ width: 390, height: 844 }); } catch { /* 旧版无此 API */ }
  const page = await renderPanel();
  try {
    const tables = [...page.el.querySelectorAll('.trace-table')];
    assert.ok(tables.length === 2, 'Skill 与 RAG 两张明细表渲染');
    for (const tb of tables) {
      assert.ok(tb.closest('.table-scroll'), '表格包在 .table-scroll 容器内（横向滚动容器化）');
    }
    assert.ok(page.el.querySelectorAll('.trace-ellipsis').length >= 2, '长文本单元格应用截断样式');
    // CSS 规则守卫（与 ws-panel-css 同口径的静态断言）
    const css = fs.readFileSync(path.join(FRONTEND, 'src', 'console.css'), 'utf8');
    assert.match(css, /\.table-scroll\s*\{[^}]*overflow-x:\s*auto/, '.table-scroll overflow-x:auto 存在');
    assert.match(css, /\.trace-ellipsis\s*\{[^}]*max-width:\s*\d+px/, '.trace-ellipsis 有 max-width 截断');
    assert.match(css, /\.trace-ellipsis\s*\{[^}]*text-overflow:\s*ellipsis/, '.trace-ellipsis 省略号');
    assert.ok(!/\.trace-table\s*\{[^}]*min-width/.test(css), '.trace-table 不得设置 min-width（页面级溢出来源）');
    const narrow = [...css.matchAll(/@media \(max-width: 700px\)/g)].some((m) => {
      const seg = css.slice(m.index, css.indexOf('}', m.index) + 40);
      return seg.includes('.trace-ellipsis');
    });
    assert.ok(narrow, '窄屏（≤700px）档有更小的截断宽度适配');
  } finally { await page.cleanup(); }
});

// ── 12. 完整 App 路由挂载：MU 用户从真实运行到达本区域 ──
test('App 路由 /mu/repos/acme/app/pr/4242：经审查管线面板渲染出调用留痕区域', async () => {
  fetchCalls.length = 0;
  ROUTES = { ...COMMON, ...traceRoutes() };
  const loaded = await loadApp();
  if (loaded.clearRuntimeConfigCache) loaded.clearRuntimeConfigCache();
  const container = domWindow.document.createElement('div');
  domWindow.document.body.appendChild(container);
  const page = await renderInto(container, React.createElement(
    MemoryRouter, { initialEntries: ['/mu/repos/acme/app/pr/4242'] }, React.createElement(loaded.App)));
  try {
    const t = page.text();
    assert.ok(t.includes('审查管线'), '审查管线面板上屏（挂载父区域）');
    assert.ok(t.includes('Skill / RAG 调用留痕'), '调用留痕区域上屏');
    assert.ok(t.includes('Skill 调用次数'), '摘要计数上屏');
    assert.ok(t.includes('org.sec-review'), '明细行上屏');
    for (const d of DISCLOSURES) assert.ok(t.includes(d), '三段文案在完整路由链中逐字上屏');
    assert.ok(countCalls(`/api/mu/runs/${RUN_ID}/skill-invocations`) >= 1, '留痕端点真实发起');
  } finally { await page.cleanup(); }
});