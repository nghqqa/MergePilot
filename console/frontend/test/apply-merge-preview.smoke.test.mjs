// apply-merge-preview.smoke.test.mjs — 应用/合并预览组件行为回归（2026-10-17 模拟卡点收口）。
// 真实渲染 ReviewArtifactsPanel（App 级 + happy-dom），锁用户可见行为而非源码字符串：
//  V1 trial1 真实响应形态（旧键 remote_error、head_match:false、remote_head:null）
//     → 抽屉显示「无法核实/无法读取」，绝不出现「已变化」；确认按钮禁用；
//  V2 可信一致（远端 head + head_match:true）→ 「版本匹配」、确认可用；
//  V3 可信不同（远端 head + head_match:false）→ 「已变化——确认将被拒绝」、确认禁用；
//  V4 合并预览失败 → 中文原因 + 下一步 + 技术详情（保留原始错误码）；
//  V5 remediation 空串 → 修复建议显示「未提供」（不编造内容）。
// 运行：cd console/frontend && node --test test/apply-merge-preview.smoke.test.mjs（需先 npm ci）
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
const MAINTAINER_ACTIONS = ['read_repository', 'read_pull_request', 'read_code_content',
  'rag_query', 'request_review', 'decide_review', 'request_repair', 'apply_fix', 'merge_pr',
  'manage_repository_binding'];
const RUN = 'run-blocked';

let ROUTES = {};
let rawBodies = new Map(); // 特例：patch.diff 需要纯文本响应
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const key = u.pathname;
  if (rawBodies.has(key)) {
    return new Response(rawBodies.get(key), { status: 200, headers: { 'content-type': 'text/plain' } });
  }
  const route = ROUTES[key];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${key}` } }), { status: 404 });
  const out = route(u, init);
  if (out instanceof Promise) return out;
  const [status, body] = out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

let loadAppPromise = null;
function loadApp() {
  loadAppPromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.amp-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\nimport { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};\nexport { App, clearRuntimeConfigCache };`);
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
    const mod = await import(pathToFileURL(bundle).href);
    return { App: mod.App ?? mod.default?.App ?? mod.default, clear: mod.clearRuntimeConfigCache };
  })();
  return loadAppPromise;
}

async function renderRoute(route) {
  const loaded = await loadApp();
  if (loaded.clear) loaded.clear();
  const container = domWindow.document.createElement('div');
  domWindow.document.body.appendChild(container);
  let root = null;
  await act(async () => {
    root = createRoot(container);
    root.render(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(loaded.App)));
  });
  for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); });
  return {
    container,
    text: () => container.textContent ?? '',
    cleanup: async () => { if (root) await act(async () => root.unmount()); container.remove(); },
  };
}
const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };
function findButtons(scope, label) {
  const needle = label.replace(/\s+/g, '');
  return [...scope.querySelectorAll('button')]
    .filter((b) => (b.textContent ?? '').replace(/\s+/g, '').includes(needle));
}

function baseRoutes({ applyPreview, mergeStatus = 409, mergeBody }) {
  return {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, { service: 'console', data_mode: 'multiuser',
      sources: { primary: 'multiuser', multiuser: { available: true } }, mu_schema_ready: true }],
    '/api/mu/session': () => [200, { user: { user_id: 'u-1', login: 'smoke1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: MAINTAINER_ACTIONS, memberships: [] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider: 'github', provider_repo_id: 'R_1' }] }],
    '/api/mu/prs/12': () => [200, {
      pull_request: { pr_id: 'pr-1', repo_id: 'r-1', provider: 'github', provider_pr_number: 12,
        head_sha: 'e'.repeat(40), branch_protection_status: 'unknown', title: 'fix' },
      review_records: [],
      latest_run: { run_id: RUN, status: 'BLOCKED', architecture_version: 'v2',
        review_mode: 'evidence_only', review_verdict: 'changes_requested',
        verification_verdict: null, tests_status: 'not_run', merge_eligibility: 'unknown' },
      my_permissions: { actions: MAINTAINER_ACTIONS } }],
    '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: [] }],
    '/api/mu/runs': () => [200, { runs: [
      { run_id: RUN, provider_pr_number: 12, status: 'BLOCKED', head_sha: 'e'.repeat(40),
        created_at: '2026-10-17T00:00:00Z', repo_id: 'r-1' }] }],
    '/api/mu/runs/run-blocked/artifacts': () => [200, {
      run: { run_id: RUN, status: 'BLOCKED', head_sha: 'e'.repeat(40), created_at: '2026-10-17T00:00:00Z',
        owner: 'acme', repo_name: 'app', provider: 'github' },
      findings: [{ finding_id: 'f-1', rule_id: 'DEMO-NULL-BUG', severity: 'P1', path: 'discount.js',
        line_start: 6, line_end: null, title: 'null TypeError when tier missing（演示发现）',
        summary_masked: 'null TypeError', remediation: '', evidence_ref: '', source: 'precheck' }],
      outputs: {},
      patch: { patch_id: 'p-1', head_sha: 'e'.repeat(40), files: [], applicability: {}, rationale: '',
        created_at: '2026-10-17T00:00:00Z', patch_version: 1, patch_digest: 'd'.repeat(64),
        correction_of: null, correction_kind: null, test_verification: null },
      feedback: [], publish: null,
      parse_state: { outputs_ok: 0, parse_failures: [] },
      verification: [{ kind: 'static_rules', label: '静态规则检查', executed: true }],
    }],
    '/api/mu/runs/run-blocked/merge-pr/preview': () => [mergeStatus, mergeBody],
    '/api/mu/runs/run-blocked/apply-fix/preview': () => [200, applyPreview],
    '/api/mu/prs/pr-1/repair': () => [409, { error: { reason: 'no_repair_pending' } }],
    '/api/mu/prs/pr-1/decision': () => [409, { error: { reason: 'no_pending' } }],
    '/api/mu/prs/pr-1/review': () => [200, { ok: true }],
  };
}

const ROUTE = '/mu/repos/acme/app/pr/12';
async function openApplyDrawer(ui) {
  await flush();
  const btn = findButtons(ui.container, '预览并应用修复')[0];
  assert.ok(btn, '①预览并应用修复按钮存在');
  assert.ok(!btn.disabled, '维护者身份按钮可用');
  await act(async () => { btn.click(); });
  await flush();
  const drawer = domWindow.document.querySelector('.ant-drawer-open');
  assert.ok(drawer, '应用预览抽屉打开');
  return drawer;
}
const confirmBtnState = (drawer) => {
  const b = [...drawer.querySelectorAll('button')].find((x) => /确认提交/.test(x.textContent ?? ''));
  return b ? Boolean(b.disabled) : null;
};
const headRowText = (drawer) => {
  // antd bordered Descriptions：label 与 content 是同 tr 内的分立单元格
  const label = [...drawer.querySelectorAll('.ant-descriptions-item-label')]
    .find((e) => (e.textContent ?? '').includes('远端当前 head'));
  const content = label?.closest('tr')?.querySelector('.ant-descriptions-item-content');
  return content ? content.textContent.replace(/\s+/g, ' ') : '';
};

after(() => { try { domWindow.close(); } catch { /* */ } setTimeout(() => process.exit(0), 300); });

test('V1 trial1 真实响应形态（旧键 remote_error，无 remote_head）→ 无法核实，绝不「已变化」，确认禁用', async () => {
  const applyPreview = { ok: true, action: 'apply_fix',
    target: { provider: 'github', repo: 'acme/app', pr_number: 12,
      source_branch: null, target_branch: null, pr_state: null,
      review_head: 'e'.repeat(40), remote_head: null, head_match: false,
      remote_error: 'No key provided to sign' },
    patch: { id: 'p-1', version: 1, digest: 'd'.repeat(64),
      applicability_state: 'not_executed', fix_test: { executed: false } },
    robot_identity: 'MergePilot GitHub App', previous_operations: [],
    can_apply: false, blocked_reason: 'remote_unreachable',
    confirm_binds: { expected_remote_head: null, expected_patch_digest: 'd'.repeat(64), expected_patch_version: 1 },
    operation_note: '', rollback_note: '' };
  ROUTES = baseRoutes({ applyPreview });
  const ui = await renderRoute(ROUTE);
  try {
    const drawer = await openApplyDrawer(ui);
    const row = headRowText(drawer);
    assert.ok(!row.includes('已变化'), '远端未知绝不出示「已变化」');
    assert.ok(row.includes('无法读取') || row.includes('无法核实远端 head'), '如实显示无法核实/无法读取');
    assert.ok(row.includes('No key provided to sign'), '保留真实原因（签名配置不可用）');
    assert.equal(confirmBtnState(drawer), true, '确认按钮禁用（不放宽）');
  } finally { await ui.cleanup(); }
});

test('V2 可信一致（远端 head + head_match:true）→ 版本匹配、确认可用', async () => {
  const applyPreview = { ok: true, action: 'apply_fix',
    target: { provider: 'github', repo: 'acme/app', pr_number: 12,
      source_branch: 'fix/x', target_branch: 'main', pr_state: 'open',
      review_head: 'e'.repeat(40), remote_head: 'e'.repeat(40), head_match: true },
    patch: { id: 'p-1', version: 1, digest: 'd'.repeat(64),
      applicability_state: 'not_executed', fix_test: { executed: false } },
    robot_identity: 'MergePilot GitHub App', previous_operations: [],
    can_apply: true, blocked_reason: null,
    confirm_binds: { expected_remote_head: 'e'.repeat(40), expected_patch_digest: 'd'.repeat(64), expected_patch_version: 1 },
    operation_note: '', rollback_note: '' };
  ROUTES = baseRoutes({ applyPreview });
  const ui = await renderRoute(ROUTE);
  try {
    const drawer = await openApplyDrawer(ui);
    const row = headRowText(drawer);
    assert.ok(row.includes('版本匹配'), '一致显示为版本匹配');
    assert.ok(!row.includes('已变化'), '一致不得显示已变化');
    assert.equal(confirmBtnState(drawer), false, '一致且可应用时确认可用（由服务端结果驱动）');
  } finally { await ui.cleanup(); }
});

test('V3 可信不同（远端 head + head_match:false）→ 已变化 + 确认禁用', async () => {
  const applyPreview = { ok: true, action: 'apply_fix',
    target: { provider: 'github', repo: 'acme/app', pr_number: 12,
      source_branch: 'fix/x', target_branch: 'main', pr_state: 'open',
      review_head: 'e'.repeat(40), remote_head: 'f'.repeat(40), head_match: false },
    patch: { id: 'p-1', version: 1, digest: 'd'.repeat(64),
      applicability_state: 'not_executed', fix_test: { executed: false } },
    robot_identity: 'MergePilot GitHub App', previous_operations: [],
    can_apply: false, blocked_reason: 'head_moved',
    confirm_binds: { expected_remote_head: 'f'.repeat(40), expected_patch_digest: 'd'.repeat(64), expected_patch_version: 1 },
    operation_note: '', rollback_note: '' };
  ROUTES = baseRoutes({ applyPreview });
  const ui = await renderRoute(ROUTE);
  try {
    const drawer = await openApplyDrawer(ui);
    const row = headRowText(drawer);
    assert.ok(row.includes('已变化——确认将被拒绝'), '可信不同才显示已变化');
    assert.ok(row.includes('f'.repeat(12).slice(0, 12)), '带远端 head 证据');
    assert.equal(confirmBtnState(drawer), true, '确认禁用');
  } finally { await ui.cleanup(); }
});

test('V4 合并预览失败 → 中文原因+下一步+技术详情（保留错误码）', async () => {
  ROUTES = baseRoutes({ applyPreview: { ok: true, target: {}, patch: {}, can_apply: false },
    mergeStatus: 409,
    mergeBody: { error: { reason: 'remote_unreachable', detail: 'No key provided to sign' } } });
  const ui = await renderRoute(ROUTE);
  try {
    await flush();
    const btn = findButtons(ui.container, '预览并合并 PR')[0];
    assert.ok(btn, '②预览并合并 PR 按钮存在');
    await act(async () => { btn.click(); });
    await flush();
    const t = ui.text();
    assert.match(t, /无法读取远端 PR 信息/, '中文原因');
    assert.match(t, /确认保持阻塞/, '影响（资格未核实，阻塞如实）');
    assert.match(t, /检查连接器/, '下一步');
    assert.match(t, /技术详情/, '技术详情入口');
    assert.match(t, /No key provided to sign/, '原始错误保留在技术详情');
    assert.ok(!/冲突|CI 失败|head 已变化/.test(t.split('技术详情')[0] ?? ''), '不把远端不可达解释为冲突/CI/head 变化');
  } finally { await ui.cleanup(); }
});

test('V5 remediation 空串 → 修复建议显示「未提供」（面板发现表展开行）', async () => {
  ROUTES = baseRoutes({ applyPreview: { ok: true, target: {}, patch: {}, can_apply: false } });
  const ui = await renderRoute(ROUTE);
  try {
    await flush();
    // 面板发现表：expandedRowRender=findingDetail——点击行首展开钮（happy-dom 下为
    // ant-table-row-expand-icon）
    const expander = ui.container.querySelector('.ant-table-row-expand-icon');
    assert.ok(expander, '发现行展开控件存在');
    await act(async () => { expander.click(); });
    await flush();
    assert.match(ui.text(), /修复建议未提供/, '空串建议如实显示未提供（不编造内容）');
  } finally { await ui.cleanup(); }
});
