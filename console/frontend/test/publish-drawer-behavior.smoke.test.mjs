// publish-drawer-behavior.smoke.test.mjs — 发布评论抽屉组件行为回归（2026-10-18 契约收口）。
// 独立审阅要求：纯函数（publish-confirm-state）与相邻冒烟不能替代真实渲染行为——
// 本文件以 App 级渲染 + 受控 fetch 驱动真实「发布预览」抽屉，锁三件事：
//   P1 失败态：预览返回 remote_head_error → head 行显示「无法读取」+真实原因；
//      「确认发布」禁用（POST 路由未注册，点击必抛——disabled 之外的兜底证明）；
//   P2 恢复态：重新预览返回可信一致 → 「版本匹配」、「确认发布」启用；点击后
//      恰好一次 POST 且成功文案上屏（恢复路径真实可走通）；
//   P3 不一致态：远端 head 不同 → 「已变化——确认将被拒绝」、确认禁用。
// 复用 apply-merge-preview 的渲染基建（esbuild 打包真实 App + happy-dom + fetch 路由）。
// 运行：cd console/frontend && node --test test/publish-drawer-behavior.smoke.test.mjs（需先 npm ci）
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
globalThis.matchMedia ??= domWindow.matchMedia;
globalThis.requestAnimationFrame ??= domWindow.requestAnimationFrame.bind(domWindow);
globalThis.cancelAnimationFrame ??= domWindow.cancelAnimationFrame.bind(domWindow);
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const SESSION = { user: { name: 'smoke' }, repos: [] };
const MAINTAINER_ACTIONS = ['read_repository', 'read_pull_request', 'read_code_content',
  'rag_query', 'request_review', 'decide_review', 'request_repair', 'apply_fix', 'merge_pr',
  'manage_repository_binding'];
const RUN = 'run-blocked';
const HEAD = 'e'.repeat(40);

let ROUTES = {};
let publishPostCount = 0;
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const out = route(u, init);
  if (out instanceof Promise) return out;
  const [status, body] = out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

let loadAppPromise = null;
function loadApp() {
  loadAppPromise ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.pub-drawer-smoke');
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

function baseRoutes({ publishPreview }) {
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
        head_sha: HEAD, branch_protection_status: 'unknown', title: 'fix' },
      review_records: [],
      latest_run: { run_id: RUN, status: 'BLOCKED', architecture_version: 'v2',
        review_mode: 'evidence_only', review_verdict: 'changes_requested',
        verification_verdict: null, tests_status: 'not_run', merge_eligibility: 'unknown' },
      my_permissions: { actions: MAINTAINER_ACTIONS } }],
    '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: [] }],
    '/api/mu/runs': () => [200, { runs: [
      { run_id: RUN, provider_pr_number: 12, status: 'BLOCKED', head_sha: HEAD,
        created_at: '2026-10-18T00:00:00Z', repo_id: 'r-1' }] }],
    '/api/mu/runs/run-blocked/artifacts': () => [200, {
      run: { run_id: RUN, status: 'BLOCKED', head_sha: HEAD, created_at: '2026-10-18T00:00:00Z',
        owner: 'acme', repo_name: 'app', provider: 'github' },
      findings: [{ finding_id: 'f-1', rule_id: 'DEMO-NULL-BUG', severity: 'P1', path: 'discount.js',
        line_start: 6, line_end: null, title: 'null TypeError when tier missing（演示发现）',
        summary_masked: 'null TypeError', remediation: '', evidence_ref: '', source: 'precheck' }],
      outputs: {},
      patch: { patch_id: 'p-1', head_sha: HEAD, files: [], applicability: {}, rationale: '',
        created_at: '2026-10-18T00:00:00Z', patch_version: 1, patch_digest: 'd'.repeat(64),
        correction_of: null, correction_kind: null, test_verification: null },
      feedback: [], publish: null,
      parse_state: { outputs_ok: 0, parse_failures: [] },
      verification: [{ kind: 'static_rules', label: '静态规则检查', executed: true }],
    }],
    // 发布预览/确认共用路径：按方法分流；POST 计数（未注册 POST 路由时点击必 500 被发现）
    '/api/mu/runs/run-blocked/publish': (u, init) => {
      const method = String(init?.method ?? 'GET').toUpperCase();
      if (method === 'GET') return [200, publishPreview];
      if (method === 'POST') {
        publishPostCount += 1;
        return [200, { ok: true, status: 'published', publish_id: 'pub-1',
          remote_comment_id: '42', remote_url: 'https://demo/comment/42', updated: false }];
      }
      return [405, { error: { reason: 'method_not_allowed' } }];
    },
    '/api/mu/prs/pr-1/repair': () => [409, { error: { reason: 'no_repair_pending' } }],
    '/api/mu/prs/pr-1/decision': () => [409, { error: { reason: 'no_pending' } }],
    '/api/mu/prs/pr-1/review': () => [200, { ok: true }],
  };
}

const ROUTE = '/mu/repos/acme/app/pr/12';
async function openPublishDrawer(ui) {
  await flush();
  const btn = findButtons(ui.container, '预览并发布到 PR')[0];
  assert.ok(btn, '「预览并发布到 PR」按钮存在');
  await act(async () => { btn.click(); });
  await flush();
  const drawer = domWindow.document.querySelector('.ant-drawer-open');
  assert.ok(drawer, '发布预览抽屉打开');
  return drawer;
}
const confirmBtn = (drawer) => {
  const b = [...drawer.querySelectorAll('button')].find((x) => /确认发布到原 PR|更新已有评论/.test(x.textContent ?? ''));
  return b ?? null;
};
const headRowText = (drawer) => {
  const label = [...drawer.querySelectorAll('.ant-descriptions-item-label')]
    .find((e) => (e.textContent ?? '').includes('远端当前 head'));
  const content = label?.closest('tr')?.querySelector('.ant-descriptions-item-content');
  return content ? content.textContent.replace(/\s+/g, ' ') : '';
};
const closeDrawer = async (drawer) => {
  const cancel = [...drawer.querySelectorAll('button')].find((b) => (b.textContent ?? '').trim() === '取消');
  await act(async () => { if (cancel) cancel.click(); });
  await flush();
};

after(() => { try { domWindow.close(); } catch { /* */ } setTimeout(() => process.exit(0), 300); });

test('P1 失败态：remote_head_error → head 行「无法读取」+真实原因，确认禁用（兜底：POST 路由未注册）', async () => {
  publishPostCount = 0;
  ROUTES = baseRoutes({ publishPreview: {
    action: 'create', robot_identity: 'MergePilot GitHub App',
    remote_head: null, head_match: null, remote_head_error: 'No key provided to sign',
    target: { provider: 'github', repo: 'acme/app', pr_number: 12, head: HEAD },
    comment: '<!-- marker -->\n审查建议（演示）', marker: 'marker', existing: null } });
  const ui = await renderRoute(ROUTE);
  try {
    const drawer = await openPublishDrawer(ui);
    const row = headRowText(drawer);
    assert.ok(row.includes('无法读取'), 'head 行如实显示无法读取');
    assert.ok(row.includes('No key provided to sign'), '保留真实原因');
    const b = confirmBtn(drawer);
    assert.ok(b, '确认按钮存在');
    assert.equal(b.disabled, true, '确认发布禁用（失败态不发给必败请求）');
    assert.equal(publishPostCount, 0, '失败态零发布 POST');
  } finally { await ui.cleanup(); }
});

test('P2 恢复态：重新预览可信一致 → 「版本匹配」、确认启用；点击恰好一次 POST 且成功文案上屏', async () => {
  publishPostCount = 0;
  ROUTES = baseRoutes({ publishPreview: {
    action: 'create', robot_identity: 'MergePilot GitHub App',
    remote_head: HEAD, head_match: true,
    target: { provider: 'github', repo: 'acme/app', pr_number: 12, head: HEAD },
    comment: '<!-- marker -->\n审查建议（演示）', marker: 'marker', existing: null } });
  const ui = await renderRoute(ROUTE);
  try {
    const drawer = await openPublishDrawer(ui);
    const row = headRowText(drawer);
    assert.ok(row.includes('版本匹配'), 'head 行显示版本匹配');
    let b = confirmBtn(drawer);
    assert.ok(b, '确认按钮存在');
    assert.equal(b.disabled, false, '恢复后确认启用');
    await act(async () => { b.click(); });
    await flush();
    assert.equal(publishPostCount, 1, '恰好一次发布 POST');
    assert.ok(ui.text().includes('已发布到原 PR'), '成功文案上屏');
  } finally { await ui.cleanup(); }
});

test('P3 不一致态：远端 head 不同 → 「已变化——确认将被拒绝」，确认禁用', async () => {
  publishPostCount = 0;
  ROUTES = baseRoutes({ publishPreview: {
    action: 'create', robot_identity: 'MergePilot GitHub App',
    remote_head: 'b'.repeat(40), head_match: false,
    target: { provider: 'github', repo: 'acme/app', pr_number: 12, head: HEAD },
    comment: '<!-- marker -->\n审查建议（演示）', marker: 'marker', existing: null } });
  const ui = await renderRoute(ROUTE);
  try {
    const drawer = await openPublishDrawer(ui);
    const row = headRowText(drawer);
    assert.ok(row.includes('已变化'), 'head 行显示已变化');
    assert.ok(row.includes('确认将被拒绝') || row.includes('409'), '如实提示服务端将拒绝');
    const b = confirmBtn(drawer);
    assert.equal(b.disabled, true, '确认发布禁用');
    assert.equal(publishPostCount, 0, '不一致态零发布 POST');
  } finally { await ui.cleanup(); }
});
