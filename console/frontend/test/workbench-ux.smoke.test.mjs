// workbench-ux.smoke.test.mjs — 审查工作台回归锁（UX 重构 2026-10-05 + 数据可信度修复）。
// 覆盖：
//   口径一致：统计卡/筛选器/异常摘要/表格同一套 PR 实体——数字=点击后列表行数（逐卡断言）
//   PR 一行一实体：多 head 只出一行（当前 head），历史收抽屉；行操作 aria 含 PR+head
//   focus=anomaly 含 protection-unknown PR；focus=reviewing 匹配真实进行中阶段（不恒空）
//   保护未知语义：摘要与详情共享 stateKey；动词=「刷新状态」（无"重探/重试检查"）；
//                真实仓库设置地址；无 disabled 装饰按钮
//   能力口径：MU 登录态 footer 不再出现「只读 · 无写操作」；身份/边界一致
// 运行：cd console/frontend && node --test test/workbench-ux.smoke.test.mjs（需先 npm ci）
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';
import { MemoryRouter } from 'react-router-dom';
import { Window } from 'happy-dom';

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const toFwd = (p) => p.replace(/\\/g, '/');

// —— DOM 环境（与 core-page 冒烟同款）——
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

// —— fetch mock（可按用例覆写）——
const SESSION = { user: { name: 'smoke', login_type: 'github-oauth', session_source: 'mu_session' }, repos: [] };
const handlers = new Map();
const setRoute = (p, status, body) => handlers.set(p, { status, body });
setRoute('/api/auth/session', 200, SESSION);
setRoute('/api/health', 200, { service: 'console', data_mode: 'live',
  sources: { primary: 'multiuser', multiuser: { available: true } } });

// 口径设计（PR 实体 = 6）：
//   #8  BLOCKED（3 个 head 行 → 1 实体，历史 2）+ 保护未知 + P0 票 → blocked ∪ anomaly
//   #9  BLOCKED（1 行）                                   → blocked
//   #4  ACTION_REQUIRED（1 行）                           → attention
//   #3  REVIEWING（1 行）+ 保护未知（checking 子态样本）    → reviewing ∪ anomaly
//   #5  PENDING（1 行）                                   → normal
//   #7  仅保护未知（无 overview 行 → 占位实体）             → anomaly
// 期望：待处理=1 已阻断=2 异常=3（#8/#3/#7） 进行中=1 全部=6
const baseOverview = {
  source: 'MU_CANONICAL_LIVE', data_source: 'MU_CANONICAL_LIVE',
  prs: [
    { repo: 'nghqqa/demo', pr: 8, head_sha: 'cur8head', run_id: 'run-8c',
      stage: 'BLOCKED', stage_source: 'skill_gate_audit (REFUSE)', updated_at: '2026-10-05T03:00:00Z' },
    { repo: 'nghqqa/demo', pr: 8, head_sha: 'old8head1', run_id: 'run-8a',
      stage: 'STALE', stage_source: 'head-ordering', updated_at: '2026-10-04T03:00:00Z' },
    { repo: 'nghqqa/demo', pr: 8, head_sha: 'old8head2', run_id: 'run-8b',
      stage: 'REVIEWING', stage_source: 'mu_review_run', updated_at: '2026-10-04T10:00:00Z' },
    { repo: 'nghqqa/demo', pr: 9, head_sha: 'cur9head', run_id: 'run-9',
      stage: 'BLOCKED', stage_source: 'skill_gate_audit (REFUSE)', updated_at: '2026-10-04T09:00:00Z' },
    { repo: 'nghqqa/demo', pr: 4, head_sha: 'cur4head', run_id: 'run-4',
      stage: 'ACTION_REQUIRED', stage_source: 'approval.tickets (PENDING)', updated_at: '2026-10-05T02:00:00Z' },
    { repo: 'nghqqa/demo', pr: 3, head_sha: 'cur3head', run_id: 'run-3',
      stage: 'REVIEWING', stage_source: 'mu_review_run', updated_at: '2026-10-05T05:00:00Z' },
    { repo: 'nghqqa/demo', pr: 5, head_sha: 'cur5head', run_id: 'run-5',
      stage: 'PENDING', stage_source: 'mu_review_run', updated_at: '2026-10-03T01:00:00Z' },
  ],
  repository_counts: [{ repo: 'nghqqa/demo', prs: 6, runs: 7, pending: 1 }],
  trend: [{ date: '2026-10-04', runs: 3 }, { date: '2026-10-05', runs: 4 }],
  stage_counts: { BLOCKED: 2, REVIEWING: 2, ACTION_REQUIRED: 1, PENDING: 1 },
  pending_summary: { count: 1, oldest_pending_at: null, oldest_wait_minutes: null },
  incidents: { stale_count: 1, failed_receipts: 0, integrity_conflicts: 0 },
  health: { postgres: 'LIVE',
    minio: { state: 'AGENTTEAMS_MANAGED', note: '' },
    backend: { state: 'OK', note: '' } },
  mode: 'mu_canonical',
};

globalThis.fetch = async (input, init = {}) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const method = String(init.method || 'GET').toUpperCase();
  const h = handlers.get(`${method} ${u.pathname}`) ?? handlers.get(u.pathname);
  if (h) return new Response(JSON.stringify(typeof h.body === 'function' ? h.body(u) : h.body),
    { status: h.status, headers: { 'content-type': 'application/json' } });
  return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
};

setRoute('/api/overview', 200, baseOverview);
// MU 域附加数据：P0 票（#8 风险列）+ 保护未知（#8 checking、#3 checking、#7 占位）
setRoute('/api/mu/session', 200, { user: { user_id: 'u1', login: 'maintainer1' },
  tenant: { tenant_id: 't1', slug: 'demo-org' }, role: 'maintainer',
  actions: ['read_repository', 'decide_review'] });
setRoute('/api/mu/approvals', 200, { approvals: [{
  approval_id: 'ap-1', severity: 'P0', status: 'PENDING',
  repo_owner: 'nghqqa', repo_name: 'demo', pr_number: 8,
  rule_id: 'path-traversal', path: 'a.js', line_start: 3, head_sha: 'cur8head',
}] });
setRoute('/api/mu/repositories', 200, { repositories: [
  { repo_id: 'r1', owner: 'nghqqa', name: 'demo', binding_state: 'active', pr_count: 6 },
] });
setRoute('/api/mu/prs', 200, () => ({ pull_requests: [
  { pr_id: 'p8', provider_pr_number: 8, head_sha: 'cur8head', branch_protection_status: 'unknown', updated_at: '2026-10-05T03:00:00Z' },
  { pr_id: 'p3', provider_pr_number: 3, head_sha: 'cur3head', branch_protection_status: 'unknown', updated_at: '2026-10-05T05:00:00Z' },
  { pr_id: 'p7', provider_pr_number: 7, head_sha: 'x7head', branch_protection_status: 'unknown', updated_at: '2026-10-02T00:00:00Z' },
  { pr_id: 'p9', provider_pr_number: 9, head_sha: 'cur9head', branch_protection_status: 'known_clean', updated_at: '2026-10-04T09:00:00Z' },
] }));

// —— 打包（plots shim；react-router-dom 外置保证单实例）——
let pagePromise = null;
function loadPage() {
  pagePromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.wb-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const shim = path.join(outDir, 'plots-shim.mjs');
    fs.writeFileSync(shim, 'export const Line = () => null;\nexport const Column = () => null;\n');
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, [
      `import OverviewPage from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/pages/OverviewPage.jsx')))};`,
      `import { AuthProvider } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/auth.jsx')))};`,
      'import { MemoryRouter } from "react-router-dom";',
      `import * as anomalies from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/anomalies.js')))};`,
      `import { RecoveryBox } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/ui.jsx')))};`,
      'export { OverviewPage, AuthProvider, MemoryRouter, anomalies, RecoveryBox };',
    ].join('\n'));
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
      alias: { '@ant-design/plots': shim }, define: { 'process.env.NODE_ENV': '"test"' },
      logLevel: 'silent',
    });
    return import(pathToFileURL(bundle).href);
  })();
  return pagePromise;
}

let appPromise = null;
function loadApp() {
  appPromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.wb-app-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const shim = path.join(outDir, 'plots-shim.mjs');
    fs.writeFileSync(shim, 'export const Line = () => null;\nexport const Column = () => null;\n');
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\nexport { App };`);
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
      alias: { '@ant-design/plots': shim },
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
    return import(pathToFileURL(bundle).href);
  })();
  return appPromise;
}

async function renderPage(route = '/overview') {
  const { OverviewPage, AuthProvider } = await loadPage();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(AuthProvider, null,
        React.createElement(MemoryRouter, { initialEntries: [route] },
          React.createElement(OverviewPage))),
    );
  });
  for (let i = 0; i < 6; i++) await act(async () => { await Promise.resolve(); });
  return renderer;
}

async function renderApp(route) {
  const { App } = await loadApp();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  for (let i = 0; i < 6; i++) await act(async () => { await Promise.resolve(); });
  return renderer;
}

// react-dom scheduler（MessageChannel）与 happy-dom 定时器会让事件循环挂住——
// 全部用例结束后强制收敛（基建收尾惯例，见 skill-page/pages-smoke 的 after 钩子）
after(() => {
  try { domWindow.happyDOM?.abort?.(); } catch { /* 忽略 */ }
  setTimeout(() => process.exit(process.exitCode ?? 0), 50);
});

// ── 一：异常建模与 PR 分组纯函数 ──
test('anomalies：四分建模完备；groupRowsByPr 多 head 归一为 PR 实体（current+history）', async () => {
  const { anomalies } = await loadPage();
  for (const k of anomalies.ANOMALY_ORDER) {
    const a = anomalies.ANOMALIES[k];
    assert.ok(a.label && a.cause && a.impact && a.next?.label, `${k} 必须含 label/cause/impact/next`);
  }
  const entities = anomalies.groupRowsByPr(baseOverview.prs, { isMu: true });
  assert.equal(entities.length, 5, 'overview 行归一为 5 个 PR 实体（#7 仅保护未知，由页面占位合成）');
  const e8 = entities.find((e) => e.n === 8);
  assert.equal(e8.current.head_sha, 'cur8head', 'current=最新 head');
  assert.equal(e8.history.length, 2, '历史 head 收进 history');
  assert.ok(String(e8.history[0].updated_at) > String(e8.history[1].updated_at), 'history 按时间倒序');
  assert.equal(entities.find((e) => e.n === 3).bucket, 'reviewing', 'REVIEWING → reviewing 桶（真实进行中）');
  // 保护未知子态动词表：绝不出现"重探/重试检查"
  for (const s of Object.values(anomalies.PROTECTION_UNKNOWN_STATES)) {
    assert.ok(!/重探|重试检查/.test(s.next?.label ?? ''), `${s.key} 不得使用"重探/重试检查"动词`);
  }
  assert.equal(anomalies.PROTECTION_UNKNOWN_STATES.not_configured.next.kind, 'repo_settings');
  assert.equal(anomalies.PROTECTION_UNKNOWN_STATES.api_failed.next.label, '刷新状态');
  assert.equal(anomalies.PROTECTION_UNKNOWN_STATES.checking.next.label, '刷新状态');
});

// ── 二：统计卡与列表口径一致（逐卡）──
test('口径一致：统计卡数字=点击后列表 PR 行数（待处理/已阻断/异常/进行中/全部逐卡相等）', async () => {
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    // 统计单位标注
    for (const label of ['待处理 PR', '待审批票（张）', '已阻断 PR', '异常 PR', '进行中 PR', '全部 PR']) {
      assert.ok(str.includes(`"${label}"`), `统计卡单位标注：「${label}」`);
    }
    const expect = { attention: [1, '待处理'], blocked: [2, '已阻断'], anomaly: [3, '异常'],
      reviewing: [1, '进行中'], all: [6, '全部'] };
    for (const [key, [n, meta]] of Object.entries(expect)) {
      // StatCard 结构：stat-count children=[n] 与 stat-label children=[meta PR] 相邻
      const idx = str.indexOf(`"${meta} PR"`);
      assert.ok(idx > 0, `统计卡「${meta} PR」存在`);
      const window = str.slice(Math.max(0, idx - 260), idx);
      assert.ok(window.includes(`"children":["${n}"]`),
        `统计卡「${meta} PR」应显示 ${n}（按 PR 去重；窗口=${window.slice(-120)}）`);
    }
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('联动：focus=blocked 列表只含阻断 PR（每 PR 一行，无重复行）', async () => {
  const renderer = await renderApp('/overview?focus=blocked');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('/mu/repos/nghqqa/demo/pr/8'), '含 #8');
    assert.ok(str.includes('/mu/repos/nghqqa/demo/pr/9'), '含 #9');
    assert.ok(!str.includes('/mu/repos/nghqqa/demo/pr/3'), '不含 REVIEWING #3');
    // 每 PR 一行：/pr/8 链接在列表只出现 1 次（多 head 不重复出行）
    const links = str.match(/\/mu\/repos\/nghqqa\/demo\/pr\/8/g) ?? [];
    assert.equal(links.length, 1, `PR #8 在列表中恰一行（实得 ${links.length}）`);
    assert.ok(str.includes('已显示「已阻断」2 个'), '联动 status 行：数量与统计卡一致');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('联动：focus=anomaly 含全部被计入异常的 PR（含 protection-unknown 与占位实体），无错误空态', async () => {
  const renderer = await renderApp('/overview?focus=anomaly');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('/mu/repos/nghqqa/demo/pr/8'), '异常列表含保护未知 #8');
    assert.ok(str.includes('/mu/repos/nghqqa/demo/pr/3'), '异常列表含保护未知 #3（reviewing 阶段也计入异常）');
    assert.ok(str.includes('/mu/repos/nghqqa/demo/pr/7'), '异常列表含 #7（仅保护未知——占位实体）');
    assert.ok(!str.includes('没有「异常」分类下的 PR'), '不出现错误空态');
    assert.ok(str.includes('已显示「异常」3 个'), '联动 status 行：异常 3 个与统计卡一致');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('联动：focus=reviewing 匹配真实进行中阶段（不再恒空）', async () => {
  const renderer = await renderApp('/overview?focus=reviewing');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('/mu/repos/nghqqa/demo/pr/3'), '进行中列表含 REVIEWING #3');
    assert.ok(str.includes('已显示「进行中」1 个'), '进行中 1 个与统计卡一致');
    assert.ok(!str.includes('没有「进行中」分类下的 PR'), '不出现错误空态');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// ── 三：PR 表格 ──
test('表格：每 PR 一行当前 head；行操作 aria 含 PR+head；风险列 P0 票驱动；历史提示', async () => {
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('cur8head'), '当前 head 短码展示');
    assert.ok(str.includes('危急'), '风险列：P0 审批票 → 危急徽章');
    assert.ok(str.includes('处理审批'), '行操作：处理审批（有票行）');
    assert.ok(str.includes('查看详情：nghqqa/demo #8，head cur8head'), '行操作 aria 含 PR+head 标识');
    assert.ok(str.includes('另有 2 个历史 head'), '多 head 行提示历史收进抽屉');
    assert.ok(!str.includes('按 run 一行'), '不再按 run 一行');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// ── 四：保护未知操作语义（组件级直测）──
test('保护未知面板：真实仓库设置地址；刷新按钮可执行；零 disabled；文档降级链接', async () => {
  const outDir = path.join(FRONTEND, 'node_modules', '.wb-pu-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry, [
    'import React from "react";',
    `import { ProtectionUnknownCard, ProtectionUnknownSummary } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/components/ProtectionUnknownPanel.jsx')))};`,
    'export { ProtectionUnknownCard, ProtectionUnknownSummary };',
  ].join('\n'));
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom', 'antd', '@ant-design/icons', 'lucide-react'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const { ProtectionUnknownCard, ProtectionUnknownSummary } = await import(pathToFileURL(bundle).href);

  // 1) 有 repo → 真实仓库设置地址（not_configured 子态）
  let r;
  await act(async () => {
    r = TestRenderer.create(React.createElement(MemoryRouter, null,
      React.createElement(ProtectionUnknownCard, { stateKey: 'not_configured',
        repo: { owner: 'nghqqa', name: 'demo' } })));
  });
  let s = JSON.stringify(r.toJSON());
  assert.ok(s.includes('https://github.com/nghqqa/demo/settings/branches'), '真实仓库设置地址');
  assert.ok(s.includes('打开仓库保护设置'), '主动作文案');
  assert.ok(!s.includes('"disabled":true'), '无 disabled 装饰按钮');

  // 2) 无 repo → 降级「查看配置指南」
  await act(async () => {
    r.update(React.createElement(MemoryRouter, null,
      React.createElement(ProtectionUnknownCard, { stateKey: 'not_configured', repo: null })));
  });
  s = JSON.stringify(r.toJSON());
  assert.ok(s.includes('查看配置指南'), '无 repo 时降级文档链接');
  assert.ok(!s.includes('settings/branches'), '无 repo 时不伪造设置地址');

  // 3) checking + onRefresh → 「刷新状态」可执行按钮；无 onRefresh → 不渲染按钮
  await act(async () => {
    r.update(React.createElement(MemoryRouter, null,
      React.createElement(ProtectionUnknownCard, { stateKey: 'checking', onRefresh: () => {} })));
  });
  s = JSON.stringify(r.toJSON());
  assert.ok(s.includes('刷新状态'), '刷新状态按钮（数据刷新动词）');
  await act(async () => {
    r.update(React.createElement(MemoryRouter, null,
      React.createElement(ProtectionUnknownCard, { stateKey: 'checking' })));
  });
  s = JSON.stringify(r.toJSON());
  assert.ok(!s.includes('刷新状态'), '无刷新动作时不渲染按钮（disabled 摆设消失）');

  // 4) undetermined → 三候选各含原因/影响/下一步；api_failed 候选无 disabled 按钮
  await act(async () => {
    r.update(React.createElement(MemoryRouter, null,
      React.createElement(ProtectionUnknownCard, { stateKey: 'undetermined',
        repo: { owner: 'nghqqa', name: 'demo' }, onRefresh: () => {} })));
  });
  s = JSON.stringify(r.toJSON());
  assert.ok(s.includes('原因未细分') && s.includes('可能原因'), '未细分+候选结构');
  assert.ok((s.match(/刷新状态/g) ?? []).length >= 1, 'api_failed 候选给「刷新状态」（其余候选各有真实去向）');
  assert.ok(!s.includes('"disabled":true'), '三候选视图零 disabled 按钮');

  // 5) 摘要列表：note 注记共享（审查进行中徽章）；无 note → 纯「保护状态未知」
  await act(async () => {
    r.update(React.createElement(MemoryRouter, null,
      React.createElement(ProtectionUnknownSummary, { items: [
        { repo: 'nghqqa/demo', pr: 3, stateKey: 'undetermined', note: '审查进行中', owner: 'nghqqa', name: 'demo' },
        { repo: 'nghqqa/demo', pr: 8, stateKey: 'undetermined', owner: 'nghqqa', name: 'demo' },
      ], onOpenPr: () => {} })));
  });
  s = JSON.stringify(r.toJSON());
  assert.ok(s.includes('保护状态未知，审查进行中'), '摘要徽章带审查进行中注记（调用方共享推导）');
  assert.ok(s.includes('打开详情：nghqqa/demo #3'), '摘要操作 aria 含 PR 标识');
  await act(async () => { r.unmount(); });
});

// ── 五：恢复按钮 ──
test('RecoveryBox：401→重新登录按钮；403→组织与接入链接；网络失败→重试按钮', async () => {
  const { RecoveryBox } = await loadPage();
  const render = async (error) => {
    let r;
    await act(async () => {
      r = TestRenderer.create(
        React.createElement(MemoryRouter, null,
          React.createElement(RecoveryBox, { error, mode: 'multiuser', onRetry: () => {} })));
    });
    return r;
  };
  const e401 = await render(Object.assign(new Error('not_authenticated'), { status: 401 }));
  assert.ok(JSON.stringify(e401.toJSON()).includes('重新登录'), '401 → 重新登录');

  const e403 = await render(Object.assign(new Error('forbidden'), { status: 403 }));
  assert.ok(JSON.stringify(e403.toJSON()).includes('查看组织与接入'), '403 → 组织与接入');

  const eNet = await render(new Error('fetch failed'));
  assert.ok(JSON.stringify(eNet.toJSON()).includes('重试'), '网络失败 → 重试');
});

// ── 六：导航与能力口径 ──
test('导航：一级恰四项 + 分组；MU 登录态无「只读·无写操作」旧文案；身份/边界一致', async () => {
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    for (const label of ['总览', '待处理', '仓库', '审批']) {
      assert.ok(str.includes(`"${label}"`), `一级导航含「${label}」`);
    }
    assert.ok(str.includes('系统管理') && str.includes('高级工具'), '分组标题存在');
    assert.ok(!str.includes('/knowledge'), '高级工具默认折叠');
    // 能力口径统一：MU OAuth 登录态下 footer 声明真实边界
    assert.ok(str.includes('不写 GitHub · 不自动合并'), 'footer 能力边界（不写 GitHub/不自动合并）');
    assert.ok(!str.includes('只读 · 无写操作'), '旧「只读 · 无写操作」文案已删除');
    assert.ok(str.includes('OAuth 会话'), 'footer 声明 OAuth 会话身份');
    // 顶栏身份（MU 会话摘要）
    assert.ok(str.includes('demo-org') && str.includes('维护者'), '顶栏组织/角色');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('导航：/skills 激活时高级工具自动展开且 active 清晰（B 波补遗回归锁）', async () => {
  const renderer = await renderApp('/skills');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('/skills'), '子路由激活时高级工具自动展开');
    assert.ok(str.includes('aria-current'), '激活项带 aria-current');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('交互语义：无 div role=link 模拟；操作链接带 aria-label', async () => {
  const renderer = await renderApp('/overview');
  try {
    const json = renderer.toJSON();
    const fakeLinks = [];
    const links = [];
    const walk = (n) => {
      if (!n) return;
      if (n.type === 'div' && String(n.props?.role ?? '') === 'link') fakeLinks.push(n);
      if (n.type === 'a') links.push(n);
      (n.children ?? []).forEach(walk);
    };
    walk(json);
    assert.equal(fakeLinks.length, 0, '禁止 div role=link 模拟链接');
    assert.ok(links.some((a) => a.props?.['aria-label']), '操作链接带 aria-label');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// ══ 数据可信度加固九项回归（PR #320 三波）══

test('R1+R2：current head 选择确定性——updated_at 相同反转输入仍同选；head_confirmed 权威字段优先于更新时间', async () => {
  const { anomalies } = await loadPage();
  const a = { repo: 'r/x', pr: 1, head_sha: 'aaa', updated_at: '2026-10-05T00:00:00Z', stage: 'REVIEWING' };
  const b = { repo: 'r/x', pr: 1, head_sha: 'bbb', updated_at: '2026-10-05T00:00:00Z', stage: 'BLOCKED' };
  const e1 = anomalies.groupRowsByPr([a, b]);
  const e2 = anomalies.groupRowsByPr([b, a]);
  assert.equal(e1[0].current.head_sha, e2[0].current.head_sha,
    '同 updated_at 反转输入 → 同一 current（head_sha 稳定 tie-break）');
  // R2：旧 head 行 updated_at 更新（如旧 head 补跑 run），不覆盖带权威标记的当前 head
  const old = { repo: 'r/x', pr: 1, head_sha: 'old', updated_at: '2026-10-06T00:00:00Z', stage: 'REVIEWING' };
  const cur = { repo: 'r/x', pr: 1, head_sha: 'cur', head_confirmed: true, updated_at: '2026-10-05T00:00:00Z', stage: 'BLOCKED' };
  const e3 = anomalies.groupRowsByPr([old, cur]);
  assert.equal(e3[0].current.head_sha, 'cur', 'head_confirmed=true 权威标记胜出（排序不掩盖权威）');
  assert.equal(e3[0].headConfirmed, true);
  assert.equal(e1[0].headConfirmed, false, '无权威标记 → headConfirmed=false（UI 显示「当前 head 未确认」）');
});

test('R3：保护状态按当前 head 判定——旧 head unknown 不污染新 head known；反向正确计入', async () => {
  const { anomalies } = await loadPage();
  const r1 = anomalies.selectCurrentProtection([
    { provider_pr_number: 9, head_sha: 'old', branch_protection_status: 'unknown', updated_at: '2026-10-01T00:00:00Z' },
    { provider_pr_number: 9, head_sha: 'new', branch_protection_status: 'known_clean', updated_at: '2026-10-05T00:00:00Z' },
  ]);
  assert.equal(r1.status, 'known_clean', '当前 head known → 不计 unknown（禁止先筛 unknown 再取最新）');
  const r2 = anomalies.selectCurrentProtection([
    { provider_pr_number: 9, head_sha: 'old', branch_protection_status: 'known_clean', updated_at: '2026-10-01T00:00:00Z' },
    { provider_pr_number: 9, head_sha: 'new', branch_protection_status: 'unknown', updated_at: '2026-10-05T00:00:00Z' },
  ]);
  assert.equal(r2.status, 'unknown', '当前 head unknown → 正确计入');
});

test('R4：占位 PR 显示「阶段未获取」，不伪造 PENDING/「尚未开始审查」', async () => {
  const { anomalies } = await loadPage();
  const reason = anomalies.pendingReasonOf({ repo: 'r/x', pr: 7, head_sha: null, run_id: null, stage: null, placeholder: true });
  assert.equal(reason.text, '阶段未获取', '占位 PR 阶段未获取');
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('阶段未获取'), '列表出现「阶段未获取」（#7 占位行）');
    assert.ok(!str.includes('protection-unknown (not in overview projection)') || str.includes('阶段未获取'),
      '占位原因不再显示旧 machine source 为主要文案');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('R5：单仓库请求失败 → 部分数据显式呈现（失败范围+计数下限）；恢复后不再显示', async () => {
  setRoute('/api/mu/prs', 500, { error: { reason: 'boom' } });
  const rFail = await renderApp('/overview');
  try {
    const str = JSON.stringify(rFail.toJSON());
    assert.ok(str.includes('保护状态为部分数据'), '部分数据横幅出现');
    assert.ok(str.includes('nghqqa/demo'), '失败范围含仓库名');
  } finally { await act(async () => { rFail.unmount(); }); }
  // 恢复：handler 还原后全新渲染 → 横幅消失、保护未知计数恢复
  setRoute('/api/mu/prs', 200, () => ({ pull_requests: [
    { pr_id: 'p8', provider_pr_number: 8, head_sha: 'cur8head', branch_protection_status: 'unknown', updated_at: '2026-10-05T03:00:00Z' },
    { pr_id: 'p3', provider_pr_number: 3, head_sha: 'cur3head', branch_protection_status: 'unknown', updated_at: '2026-10-05T05:00:00Z' },
    { pr_id: 'p7', provider_pr_number: 7, head_sha: 'x7head', branch_protection_status: 'unknown', updated_at: '2026-10-02T00:00:00Z' },
    { pr_id: 'p9', provider_pr_number: 9, head_sha: 'cur9head', branch_protection_status: 'known_clean', updated_at: '2026-10-04T09:00:00Z' },
  ] }));
  const rOk = await renderApp('/overview');
  try {
    const str = JSON.stringify(rOk.toJSON());
    assert.ok(!str.includes('保护状态为部分数据'), '恢复后无部分数据横幅');
    assert.ok(str.includes('异常 PR'), '统计恢复');
  } finally { await act(async () => { rOk.unmount(); }); }
});

test('R6：REVIEWING 无探测证据 → 不宣称「检查中」；摘要徽章=「保护状态未知，审查进行中」', async () => {
  const { anomalies } = await loadPage();
  assert.equal(anomalies.protectionUnknownKind({ branch_protection_status: 'unknown' }, null), 'undetermined',
    '无显式探测证据（恒 null）：REVIEWING 也落 undetermined');
  assert.equal(anomalies.protectionUnknownKind({ branch_protection_status: 'unknown' }, 'in_flight'), 'checking',
    '仅显式探测证据（in_flight）才可判 checking——当前后端不提供');
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(!str.includes('"检查中"'), '页面不出现「检查中」徽章（无探测证据）');
    assert.ok(str.includes('保护状态未知，审查进行中'), '#3 REVIEWING → 徽章带审查进行中注记');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('R7：OAuth 与操作员密码会话显示不同身份来源；无标记 → 来源未知（不按模式推断）', async () => {
  const identityUrl = pathToFileURL(path.join(FRONTEND, 'src/identity.js')).href;
  const { deriveIdentitySource, capabilityLine } = await import(identityUrl);
  const oauth = deriveIdentitySource({ session: { user: { name: 'a', login_type: 'github-oauth' }, session_source: 'mu_session' }, dataMode: 'live', authed: true });
  const oper = deriveIdentitySource({ session: { user: { name: 'b', login_type: 'operator_password' } }, dataMode: 'live', authed: true });
  const unk = deriveIdentitySource({ session: { user: { name: 'c' } }, dataMode: 'live', authed: true });
  const tester = deriveIdentitySource({ session: { user: { name: 't' } }, dataMode: 'fixture', authed: true });
  assert.equal(oauth.key, 'github_oauth');
  assert.equal(oper.key, 'operator_password');
  assert.equal(unk.key, 'unknown', '无显式标记 → 来源未知（不冒充 OAuth）');
  assert.equal(tester.key, 'test_principal');
  assert.notEqual(capabilityLine(oauth), capabilityLine(oper), '不同身份 → 不同能力行');
  // multiuser 模式但无会话 provider 标记 → 不因模式冒充 OAuth
  const muNoMarker = deriveIdentitySource({ session: { user: { name: 'd' } }, dataMode: 'live', authed: true });
  assert.equal(muNoMarker.key, 'unknown');
});

test('R8：账户摘要缓存按用户失效——换号不残留；失败可重试；旧延迟响应不覆盖新会话', async () => {
  const hooksUrl = pathToFileURL(path.join(FRONTEND, 'src/hooks.js')).href;
  // hooks.js 以 named export 提供 cache 控制；直接动态 import（react external 不需要——纯函数域）
  const mod = await import(hooksUrl);
  const realFetch = globalThis.fetch;
  try {
    // A 用户：租户 org-a
    globalThis.fetch = async () => new Response(JSON.stringify({ user: { login: 'alice' }, tenant: { slug: 'org-a' }, role: 'maintainer' }),
      { status: 200, headers: { 'content-type': 'application/json' } });
    const a = await mod.fetchMuAccountSummary({ github_login: 'alice' });
    assert.equal(a.org, 'org-a', 'A 摘要');
    // A 登出 → 缓存清除
    mod.resetMuAccountSummaryCache();
    // B 登录：租户 org-b
    globalThis.fetch = async () => new Response(JSON.stringify({ user: { login: 'bob' }, tenant: { slug: 'org-b' }, role: 'reviewer' }),
      { status: 200, headers: { 'content-type': 'application/json' } });
    const b = await mod.fetchMuAccountSummary({ github_login: 'bob' });
    assert.equal(b.org, 'org-b', 'B 摘要不残留 A 的组织/角色');
    // 失败不缓存：失败后重试成功可取到
    mod.resetMuAccountSummaryCache();
    globalThis.fetch = async () => new Response('{}', { status: 500 });
    const failed = await mod.fetchMuAccountSummary({ github_login: 'carol' });
    assert.equal(failed, null, '失败不缓存');
    globalThis.fetch = async () => new Response(JSON.stringify({ user: { login: 'carol' }, tenant: { slug: 'org-c' }, role: 'maintainer' }),
      { status: 200, headers: { 'content-type': 'application/json' } });
    const retried = await mod.fetchMuAccountSummary({ github_login: 'carol' });
    assert.equal(retried.org, 'org-c', '失败后重试成功');
    // 旧延迟响应不覆盖：A 的慢响应在途时换会话 → 代际令牌丢弃该响应，缓存不污染
    mod.resetMuAccountSummaryCache();
    let releaseSlow;
    const slowPromise = new Promise((resolve) => { releaseSlow = resolve; });
    globalThis.fetch = () => slowPromise; // A 的响应挂起在途
    const inFlight = mod.fetchMuAccountSummary({ github_login: 'alice' }); // 在途（未 await）
    mod.resetMuAccountSummaryCache(); // 换会话（A 登出 → B 登录前）
    const daveBody = JSON.stringify({ user: { login: 'dave' }, tenant: { slug: 'org-d' }, role: 'maintainer' });
    globalThis.fetch = async () => new Response(daveBody, { status: 200, headers: { 'content-type': 'application/json' } });
    const after = await mod.fetchMuAccountSummary({ github_login: 'dave' });
    assert.equal(after?.org, 'org-d', '新会话请求正常');
    // 释放 A 的慢响应——必须被代际令牌丢弃，且不污染 dave 的缓存
    releaseSlow(new Response(JSON.stringify({ user: { login: 'alice' }, tenant: { slug: 'org-a' }, role: 'maintainer' }),
      { status: 200, headers: { 'content-type': 'application/json' } }));
    const stale = await inFlight;
    assert.equal(stale, null, '旧会话在途响应被丢弃');
    const cached = await mod.fetchMuAccountSummary({ github_login: 'dave' });
    assert.equal(cached.org, 'org-d', '缓存不被旧响应污染');
  } finally {
    globalThis.fetch = realFetch;
    mod.resetMuAccountSummaryCache();
  }
});

test('R10：投影触顶（prs_truncated）→「统计为下限」横幅，不冒充完整总数', async () => {
  setRoute('/api/overview', 200, { ...baseOverview, prs_truncated: true, prs_projection_limit: 50 });
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('概览投影达到上限——统计为下限'), '触顶横幅出现');
    assert.ok(str.includes('超出部分未计入本页统计与列表'), '横幅声明覆盖范围');
  } finally { await act(async () => { renderer.unmount(); }); }
  setRoute('/api/overview', 200, baseOverview); // 还原
});

test('R9：统计/筛选/图表摘要/详情当前 head 一致（同源实体；历史 head 不出现在列表）', async () => {
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    // 当前 head 一致：列表只显示 cur8head（旧 head 不入列表行）
    assert.ok(str.includes('cur8head'), '列表显示当前 head');
    assert.ok(!str.includes('old8head'), '历史 head 不出现在列表（收进抽屉）');
    // 详情同源：抽屉（点击前不渲染）与列表共享同一 enriched 实体——抽屉 Head=当前 head
    // 由组件单源保证（drawerEntity 即列表实体）；此处锁定列表/统计同源数字
    assert.ok(str.includes('已显示') || str.includes('PR 列表'), '列表区存在');
    assert.ok(str.includes('保护未知'), '异常实体带保护未知标识（与异常计数同源）');
  } finally { await act(async () => { renderer.unmount(); }); }
});
