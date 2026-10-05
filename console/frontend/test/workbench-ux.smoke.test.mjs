// workbench-ux.smoke.test.mjs — 审查工作台 UX 收敛重构回归锁（2026-10-05）。
// 覆盖任务书八板块的可断言核心：
//   一 信息架构   ：一级导航四项 + 系统管理组 + 高级工具默认折叠/子路由自动展开
//   二 异常状态   ：四分建模（原因/影响/下一步）+ 401/403/网络恢复按钮
//   五 图表       ：语义色 scale range + 文本摘要（可访问摘要不依赖颜色）
//   六 PR 表格    ：默认列 PR/风险/阶段/待处理原因/更新时间/操作 + 抽屉承载 Head/Run/来源
//   七 交互语义   ：统计卡/行操作为真实元素；分页 aria-label；无 div role=link 模拟
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
const SESSION = { user: { name: 'smoke', repos: [] } };
const handlers = new Map();
const setRoute = (p, status, body) => handlers.set(p, { status, body });
setRoute('/api/auth/session', 200, SESSION);
setRoute('/api/health', 200, { service: 'console', data_mode: 'live',
  sources: { primary: 'multiuser', multiuser: { available: true } } });

const baseOverview = {
  source: 'MU_CANONICAL_LIVE', data_source: 'MU_CANONICAL_LIVE',
  prs: [
    { repo: 'nghqqa/demo', pr: 8, head_sha: 'a1b2c3d4e5f6', run_id: 'run-8',
      stage: 'ACTION_REQUIRED', stage_source: 'approval.tickets (PENDING)',
      updated_at: '2026-10-05T03:00:00Z' },
    { repo: 'nghqqa/demo', pr: 9, head_sha: 'b1b2c3d4e5f6', run_id: 'run-9',
      stage: 'BLOCKED', stage_source: 'skill_gate_audit (REFUSE)',
      updated_at: '2026-10-04T09:00:00Z' },
    { repo: 'nghqqa/other', pr: 3, head_sha: 'c1b2c3d4e5f6', run_id: 'run-3',
      stage: 'REVIEWING', stage_source: 'mu_review_run',
      updated_at: '2026-10-05T05:00:00Z' },
  ],
  repository_counts: [{ repo: 'nghqqa/demo', prs: 2, runs: 2, pending: 1 }],
  trend: [{ date: '2026-10-04', runs: 2 }, { date: '2026-10-05', runs: 1 }],
  stage_counts: { ACTION_REQUIRED: 1, BLOCKED: 1, REVIEWING: 1 },
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

// MU 域附加数据（风险列 P0 票 + 保护未知行）
setRoute('/api/mu/session', 200, { user: { user_id: 'u1', login: 'maintainer1' },
  tenant: { tenant_id: 't1', slug: 'demo-org' }, role: 'maintainer',
  actions: ['read_repository', 'decide_review'] });
setRoute('/api/mu/approvals', 200, { approvals: [{
  approval_id: 'ap-1', severity: 'P0', status: 'PENDING',
  repo_owner: 'nghqqa', repo_name: 'demo', pr_number: 8,
  rule_id: 'path-traversal', path: 'a.js', line_start: 3, head_sha: 'a1b2c3d4e5f6',
}] });
setRoute('/api/mu/repositories', 200, { repositories: [
  { repo_id: 'r1', owner: 'nghqqa', name: 'demo', binding_state: 'active', pr_count: 2 },
] });
setRoute('/api/mu/prs', 200, (u) => ({ pull_requests: u.searchParams.get('repo_id') === 'r1'
  ? [{ pr_id: 'p8', provider_pr_number: 8, head_sha: 'a1', branch_protection_status: 'unknown', updated_at: '2026-10-05T03:00:00Z' },
     { pr_id: 'p9', provider_pr_number: 9, head_sha: 'b1', branch_protection_status: 'known_clean', updated_at: '2026-10-04T09:00:00Z' }]
  : [] }));
setRoute('/api/overview', 200, baseOverview);

// —— 打包（plots shim）——
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
      alias: { '@ant-design/plots': path.join(outDir, 'plots-shim.mjs') },
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
    return import(pathToFileURL(bundle).href);
  })();
  return appPromise;
}

async function renderPage(route = '/overview') {
  const { OverviewPage, AuthProvider, MemoryRouter } = await loadPage();
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

// ── 一：异常建模纯函数 ──
test('anomalies：四分建模完备——每类含原因/影响/下一步；deriveAnomalies 计数正确', async () => {
  const { anomalies } = await loadPage();
  for (const k of anomalies.ANOMALY_ORDER) {
    const a = anomalies.ANOMALIES[k];
    assert.ok(a.label && a.cause && a.impact && a.next?.label, `${k} 必须含 label/cause/impact/next`);
  }
  const list = anomalies.deriveAnomalies(
    { incidents: { stale_count: 2, failed_receipts: 1, integrity_conflicts: 3 } }, 4);
  assert.deepEqual(list.map((x) => x.count), [2, 1, 3, 4]);
});

test('anomalies：保护未知四分子态可推导（checking 优先，其余 undetermined 且列三候选）', async () => {
  const { anomalies } = await loadPage();
  assert.equal(anomalies.protectionUnknownKind({}, true), 'checking');
  const kind = anomalies.protectionUnknownKind({ branch_protection_status: 'unknown' }, false);
  assert.equal(kind, 'undetermined');
  assert.deepEqual(anomalies.PROTECTION_UNKNOWN_STATES.undetermined.candidates,
    ['not_configured', 'permission', 'api_failed']);
  // 每个子态都必须给原因/影响/下一步（不允许只显示状态文字）
  for (const s of Object.values(anomalies.PROTECTION_UNKNOWN_STATES)) {
    assert.ok(s.cause && s.impact && s.next?.label, `${s.key} 必须含 cause/impact/next`);
  }
});

test('anomalies：bucketOf / pendingReasonOf 阶段→工作台桶与人话原因', async () => {
  const { anomalies } = await loadPage();
  assert.equal(anomalies.bucketOf({ stage: 'ACTION_REQUIRED' }), 'attention');
  assert.equal(anomalies.bucketOf({ stage: 'BLOCKED' }), 'blocked');
  assert.equal(anomalies.bucketOf({ stage: 'STALE' }), 'anomaly');
  assert.equal(anomalies.bucketOf({ stage: 'UNKNOWN' }), 'anomaly');
  assert.equal(anomalies.bucketOf({ stage: 'PASSED' }), 'normal');
  const r = anomalies.pendingReasonOf({ stage: 'BLOCKED', stage_source: 'skill_receipt_outbox.integrity' });
  assert.ok(r.text.includes('完整性冲突'), 'integrity 冲突的人话原因');
  const g = anomalies.pendingReasonOf({ stage: 'BLOCKED', stage_source: 'skill_gate_audit (REFUSE)' });
  assert.ok(g.text.includes('拒绝') || g.text.includes('受控'), 'gate 拒绝的人话原因');
});

// ── 二：/overview 工作台首屏 ──
test('工作台首屏：统计卡=可点击入口（button 真实元素）且与列表联动口径一致', async () => {
  const renderer = await renderPage('/overview');
  try {
    const json = renderer.toJSON();
    const str = JSON.stringify(json);
    assert.ok(str.includes('待处理（需我处理）'), '首屏统计卡：待处理');
    assert.ok(str.includes('已阻断'), '首屏统计卡：已阻断（主入口）');
    assert.ok(str.includes('异常'), '首屏统计卡：异常');
    // 统计卡必须是真实 button（含 aria-pressed），不允许 div role=link 模拟
    const buttons = [];
    const walk = (n) => {
      if (!n) return;
      if (n.type === 'button') buttons.push(n);
      (n.children ?? []).forEach(walk);
    };
    walk(json);
    const statButtons = buttons.filter((b) => String(b.props?.className ?? '').includes('stat-card'));
    assert.ok(statButtons.length >= 4, `统计卡应为真实 button 元素（实得 ${statButtons.length}）`);
    assert.ok(statButtons.every((b) => 'aria-pressed' in b.props), '统计卡带 aria-pressed 当前态');
    // 趋势/仓库分布文本摘要存在（不依赖颜色的可访问摘要）
    assert.ok(str.includes('近 14 天共'), '趋势图文本摘要');
    assert.ok(str.includes('个仓库'), '仓库分布文本摘要');
  } finally {
    await act(async () => { renderer.unmount(); });
  }
});

test('工作台联动：?focus=blocked 时列表只含已阻断行；?focus=anomaly 含过期行', async () => {
  const blocked = await renderPage('/overview?focus=blocked');
  try {
    const str = JSON.stringify(blocked.toJSON());
    assert.ok(str.includes('#9'), 'blocked 筛选含 BLOCKED PR #9');
    assert.ok(!str.includes('#3'), 'blocked 筛选不含 REVIEWING PR #3');
    assert.ok(str.includes('PR 列表（') && str.includes('已阻断'), '列表标题反映当前筛选');
  } finally { await act(async () => { blocked.unmount(); }); }
  const anomaly = await renderPage('/overview?focus=anomaly');
  try {
    // anomalies 来自 incidents.stale_count=1：工作台桶含 STALE/UNKNOWN 阶段行
    // 本 fixture 无 STALE 行——异常区四分卡显示计数（含保护未知），列表空态诚实
    const str = JSON.stringify(anomaly.toJSON());
    assert.ok(str.includes('没有「异常」分类下的 PR'), '异常筛选空态诚实提示（不冒充）');
  } finally { await act(async () => { anomaly.unmount(); }); }
});

test('异常区：四类异常各显示 原因/影响/下一步（dt=原因/影响/下一步）', async () => {
  const renderer = await renderPage('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('Head 已过期（stale）'), 'stale 异常条目');
    assert.ok(str.includes('保护状态未知'), 'protection_unknown 异常条目');
    assert.ok(str.includes('原因'), '异常卡原因字段');
    assert.ok(str.includes('影响'), '异常卡影响字段');
    assert.ok(str.includes('下一步'), '异常卡下一步字段');
    assert.ok(str.includes('查看过期项'), 'stale 下一步按钮文案');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// ── 三：PR 表格 ──
test('表格列：PR/风险/阶段/待处理原因/更新时间/操作；Head 列移出默认列', async () => {
  const renderer = await renderPage('/overview');
  try {
    const json = renderer.toJSON();
    const str = JSON.stringify(json);
    for (const h of ['PR', '风险', '阶段', '待处理原因', '更新时间', '操作']) {
      assert.ok(str.includes(`"${h}"`), `表头含「${h}」`);
    }
    assert.ok(!str.includes('"Head"'), 'Head 列已移出默认列（进抽屉）');
    assert.ok(str.includes('查看详情'), '行操作：查看详情');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// MU 集成（风险列/处理审批/深链）依赖 ConfigCtx（真实 App 配置路径）——走完整 App 渲染
test('MU 集成：P0 票驱动风险列与处理审批操作；PR 深链 /mu/repos/...', { timeout: 30000 }, async () => {
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('危急'), '风险列：P0 审批票 → 危急徽章');
    assert.ok(str.includes('处理审批'), '行操作：处理审批（有票行）');
    assert.ok(str.includes('/mu/repos/nghqqa/demo/pr/8'), 'MU 部署 PR 深链 /mu/repos/...');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('交互语义：表格无 div role=link 模拟；操作链接带 aria-label', async () => {
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
    const labeled = links.filter((a) => a.props?.['aria-label']);
    assert.ok(labeled.length > 0, '操作链接带 aria-label');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// ── 四：恢复按钮（401/403/网络） ──
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
  const s401 = JSON.stringify(e401.toJSON());
  assert.ok(s401.includes('重新登录'), '401 → 重新登录（可执行按钮）');
  assert.ok(s401.includes('登录已过期'), '401 → 状态说明');

  const e403 = await render(Object.assign(new Error('forbidden'), { status: 403 }));
  const s403 = JSON.stringify(e403.toJSON());
  assert.ok(s403.includes('查看组织与接入'), '403 → 组织与接入入口');
  assert.ok(s403.includes('/multiuser'), '403 → 可执行去向链接');

  const eNet = await render(new Error('fetch failed'));
  const sNet = JSON.stringify(eNet.toJSON());
  assert.ok(sNet.includes('重试'), '网络失败 → 重试按钮');
  assert.ok(sNet.includes('数据源不可达'), '网络失败 → 诚实状态名');
});

// ── 五：导航结构 ──
test('导航：一级恰四项（总览/待处理/仓库/审批）+ 系统管理组 + 高级工具默认折叠', async () => {
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    for (const label of ['总览', '待处理', '仓库', '审批']) {
      assert.ok(str.includes(`"${label}"`), `一级导航含「${label}」`);
    }
    assert.ok(str.includes('系统管理'), '系统管理分组标题');
    assert.ok(str.includes('高级工具'), '高级工具折叠组标题');
    // 默认折叠：/overview 下高级工具子项不可见
    assert.ok(!str.includes('/knowledge'), '高级工具默认折叠（/knowledge 链接不在树中）');
    // 运行记录沉入系统管理（MU 源下隐藏）
    assert.ok(!str.includes('"/runs"'), 'MU 实时源下 /runs 导航隐藏（snapshot 取证域）');
    // 短标签 + tooltip：多用户实时 → 「实时」，完整含义在 title
    assert.ok(str.includes('多用户实时（按组织隔离）：登录组织的实时数据'), '短标签 tooltip 携带完整含义');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('导航：/skills 激活时高级工具自动展开且 active 清晰（B 波补遗回归锁）', async () => {
  const renderer = await renderApp('/skills');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('/skills'), '子路由激活时高级工具自动展开（/skills 入口在树中）');
    assert.ok(str.includes('aria-current'), '激活项带 aria-current');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('导航：组织与接入在系统管理组（一级不出现）；顶栏含组织/角色摘要', async () => {
  const renderer = await renderApp('/overview');
  try {
    const str = JSON.stringify(renderer.toJSON());
    assert.ok(str.includes('组织与接入'), '组织与接入入口存在');
    assert.ok(str.includes('demo-org'), '顶栏账户区显示组织 slug（MU 会话摘要）');
    assert.ok(str.includes('维护者'), '顶栏账户区显示角色中文标签');
  } finally { await act(async () => { renderer.unmount(); }); }
});
