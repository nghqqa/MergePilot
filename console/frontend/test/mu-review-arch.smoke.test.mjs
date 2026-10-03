// mu-review-arch.smoke.test.mjs — ADR-002 PR F 前端冒烟：
// 审查架构面板（三档/consent 门/出站披露）+ PR 详情四分立判定上屏。
// 复用 pages-smoke 基建（esbuild 打包真实 App + react-test-renderer + happy-dom）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { after } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';
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

const SESSION = { user: { name: 'smoke' }, repos: [] };
let ROUTES = {};
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-rarch-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const entry = path.join(outDir, 'entry.mjs');
  const bundle = path.join(outDir, 'bundle.cjs');
  fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};
import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};\nexport { App, clearRuntimeConfigCache };`);
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  const pick = (k) => mod[k] ?? mod.default?.[k] ?? null;
  return { App: pick('App'), clear: pick('clearRuntimeConfigCache') };
}

const COMMON = {
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, {
    service: 'console', data_mode: 'fixture',
    sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } },
  }],
};

async function renderRoute(route) {
  const loaded = await loadApp();
  const App = loaded.App;
  if (loaded.clear) loaded.clear();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}

// 可见文本（字符串子节点拼接；不含 title 等属性——PR-2 起 raw enum 允许留在 title/技术详情）
function walk(node, fn) {
  if (node == null) return;
  fn(node);
  if (typeof node !== 'object') return;
  (node.children ?? []).forEach((c) => walk(c, fn));
}
function allText(node) {
  let t = '';
  walk(node, (nd) => { if (typeof nd === 'string') t += nd; });
  return t;
}

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

test('审查架构面板：三档模式+consent 门+出站披露上屏（管理员）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'admin' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'platform_admin',
      actions: ['read_repository', 'read_pull_request', 'manage_instance'],
      memberships: [],
    }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001',
        binding_id: 'b-1', binding_kind: 'fixture', installation_state: 'active' }] }],
    '/api/mu/prs': () => [200, { pull_requests: [
      { pr_id: 'pr-1', provider_pr_number: 4242, head_sha: 'ab'.repeat(20) }] }],
    '/api/mu/github/app/status': () => [200, { configured: true }],
    '/api/mu/github/installations': () => [200, { installations: [{ id: 1, revoked: false, suspended: false }] }],
    '/api/mu/review-policy': () => [200, {
      arch_enabled: true,
      modes: ['evidence_only', 'external_api', 'local'],
      policy: { policy_version: 3, review_mode: 'external_api', provider_id: 'deepseek',
        model_id: 'deepseek-chat', code_egress_allowed: true, consent_version: 'cv-deepseek-v1',
        retention_ack: true },
    }],
    '/api/mu/providers': () => [200, { providers: [{
      provider_id: 'deepseek', display_name: 'DeepSeek', endpoint_origin: 'api.deepseek.com',
      policy_status: 'custom_acknowledged', retention_summary: '30 天',
      training_summary: '不用于训练', region_summary: '中国', policy_reference: 'https://example',
      state: 'listed' }] }],
    '/api/mu/egress-events': () => [200, { events: [{
      event_id: 1, run_id: 'run-1', agent_role: 'reviewer', provider_id: 'deepseek',
      model_id: 'deepseek-chat', head_sha: 'abc123', input_digest: 'd'.repeat(32),
      response_digest: null, files: ['src/a.js'], bytes_sent: 2048, tokens_sent: 512,
      redactions_applied: 3, policy_version: 3, consent_version: 'cv-deepseek-v1',
      timeout: false, retry_count: 0, created_at: '2026-09-29T10:00:00Z' }] }],
  };
  const { json } = await renderRoute('/multiuser');
  const text = json();
  try {
    // 三档模式文案在屏（Segmented 全量 options 渲染）
    assert.ok(text.includes('仅证据审查（零出站）'), 'evidence_only 档文案');
    assert.ok(text.includes('外部 API 审查'), 'external_api 档文案');
    assert.ok(text.includes('本地接口（预留）'), 'local 档文案');
    // consent 状态与门
    assert.ok(text.includes('已同意出站'), 'consent 状态上屏');
    assert.ok(text.includes('撤销出站同意'), 'consent 可撤销入口');
    // Provider 披露要素
    assert.ok(text.includes('数据保留'), 'Provider 披露（保留）');
    assert.ok(text.includes('不用于训练'), 'Provider 披露（训练）');
    // 出站披露列表
    assert.ok(text.includes('出站披露'), '出站披露区在');
    assert.ok(text.includes('d'.repeat(8)), 'digest 片段上屏');
    assert.ok(text.includes('2,048') || text.includes('2048'), '字节计数上屏');
    // 红线：无 key/端点输入框文案
    assert.ok(text.includes('API Key 与端点属于部署级机密'), '机密红线文案');
  } catch (e) { console.log(text.slice(0, 3000)); throw e; }
});

test('审查架构面板：v1 只读态（arch_enabled=false）如实提示', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'admin' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'platform_admin', actions: ['manage_instance'], memberships: [],
    }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001',
        binding_id: 'b-1', binding_kind: 'fixture', installation_state: 'active' }] }],
    '/api/mu/prs': () => [200, { pull_requests: [
      { pr_id: 'pr-1', provider_pr_number: 4242, head_sha: 'ab'.repeat(20) }] }],
    '/api/mu/github/app/status': () => [200, { configured: true }],
    '/api/mu/github/installations': () => [200, { installations: [{ id: 1, revoked: false, suspended: false }] }],
    '/api/mu/review-policy': () => [200, {
      arch_enabled: false, modes: ['evidence_only'],
      policy: { policy_version: 1, review_mode: 'evidence_only' },
    }],
    '/api/mu/providers': () => [200, { providers: [] }],
    '/api/mu/egress-events': () => [403, { error: { reason: 'forbidden' } }],
  };
  const { json } = await renderRoute('/multiuser');
  const text = json();
  assert.ok(text.includes('架构 v2 未启用——只读'), 'v1 只读态如实标注');
});

test('PR 详情：四分立判定+出站计数上屏（互不冒充）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'rev1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ['read_pull_request'], memberships: [],
    }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001' }] }],
    '/api/mu/prs/4242': () => [200, {
      pull_request: { pr_id: 'pr-1', provider_pr_number: 4242, head_sha: 'ab'.repeat(20),
        branch_protection_status: 'unknown', title: 'fix' },
      review_records: [],
      latest_run: { run_id: 'run-1', status: 'VERIFYING', architecture_version: 'v2',
        review_mode: 'external_api', execution_mode: 'external_api',
        review_verdict: 'changes_requested', verification_verdict: 'inconclusive',
        tests_status: 'passed', merge_eligibility: 'ineligible', code_egress: 2,
        model_judgment: { verdict: 'PASS', input: 'digest_only', note: '仅审计留痕' },
        test_evidence: 'tools:static_check=ok,secret_scan=ok' },
      my_permissions: { actions: [] },
    }],
    '/api/mu/runs': () => [200, { runs: [] }],
  };
  const { json } = await renderRoute('/mu/repos/acme/app/pr/4242');
  const text = json();
  try {
    assert.ok(text.includes('审查结论'), '审查结论标签');
    assert.ok(text.includes('要求修改'), 'review_verdict=changes_requested 值上屏');
    assert.ok(text.includes('验证结论（模型域）') && text.includes('不确定'), 'verification_verdict=inconclusive 上屏（分显模型域）');
    assert.ok(text.includes('测试证据（工具域）') && text.includes('通过'), 'tests_status=passed 上屏（分显工具域）');
    assert.ok(text.includes('合并资格') && text.includes('不具备资格'), 'merge_eligibility=ineligible 上屏');
    assert.ok(text.includes('模型判定（原始）') && text.includes('PASS') && text.includes('digest_only'), 'model_judgment 原始判定分显');
    assert.ok(text.includes('工具证据：') && text.includes('static_check=ok'), 'test_evidence 工具证据分显');
    assert.ok(text.includes('次调用（审计在案）'), '出站计数上屏');
    assert.ok(text.includes('模型未读取完整 patch'), 'RC 语义措辞上屏（模型未读完整 patch/inconclusive/工具独立有效）');
  } catch (e) { console.log(text.slice(0, 3000)); throw e; }
});

// ── v16 高危修复审批门（fix/high-risk-fix-approval-gate）：WAITING run + PENDING 票面板 ──
test('PR 详情：WAITING_FOR_HUMAN_APPROVAL → 审批面板（逐条票+DRY_RUN 声明+禁词零出现）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'maint1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ['read_pull_request', 'decide_review'], memberships: [],
    }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001' }] }],
    '/api/mu/prs/4242': () => [200, {
      pull_request: { pr_id: 'pr-1', provider_pr_number: 4242, head_sha: 'ab'.repeat(20),
        branch_protection_status: 'unknown', title: 'fix' },
      review_records: [],
      latest_run: { run_id: 'run-1', status: 'WAITING_FOR_HUMAN_APPROVAL',
        architecture_version: 'v2', review_mode: 'external_api',
        review_verdict: 'changes_requested', merge_eligibility: 'unknown' },
      my_permissions: { actions: ['decide_review'] },
    }],
    '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: [
      { approval_id: 'ap-1', run_id: 'run-1', finding_id: 'f-1', severity: 'P0',
        status: 'PENDING', head_sha: 'ab'.repeat(20), expires_at: '2026-10-09T00:00:00Z',
        decided_by: null, decided_at: null, pr_number: 4242,
        rule_id: 'R-SECRET', path: 'a.js', line_start: 3, summary_masked: 'sk-***' },
      { approval_id: 'ap-2', run_id: 'run-1', finding_id: 'f-2', severity: 'P1',
        status: 'CONSUMED', head_sha: 'ab'.repeat(20), expires_at: '2026-10-09T00:00:00Z',
        decided_by: 'mu:very-long-maintainer-identifier', decided_at: '2026-10-02T14:43:00Z', pr_number: 4242,
        rule_id: 'R-SQL', path: 'b.js', line_start: 5, summary_masked: 'q***' } ] }],
    '/api/mu/runs': () => [200, { runs: [] }],
  };
  const { json } = await renderRoute('/mu/repos/acme/app/pr/4242');
  const text = json();
  try {
    assert.ok(text.includes('高危修复审批'), '审批面板标题');
    assert.ok(text.includes('R-SECRET') && text.includes('a.js'), '发现摘要（rule/path）');
    assert.ok(text.includes('批准受控修复') && text.includes('拒绝修复'), '批准/拒绝按钮');
    assert.ok(text.includes('DRY_RUN 修复建议') && text.includes('不自动合并'), 'DRY_RUN/不合并声明');
    assert.ok(text.includes('Fixer 被阻塞'), 'Fixer 阻塞提示');
    assert.ok(text.includes('已消费'), 'CONSUMED 状态上屏（短标签）');
    assert.ok(!text.includes('DRY_RUN 已启动'), 'CONSUMED 标签不携带超长后缀（溢出回归守卫）');
    assert.ok(text.includes('mu:very-long-maintainer-identifier') && text.includes('2026-10-02 14:43'), '决定人+时间分两行上屏');
    assert.ok(!text.includes('已自动修复') && !text.includes('已修复漏洞') && !text.includes('可直接合并'), '禁词零出现');
  } catch (e) { console.log(text.slice(0, 3000)); throw e; }
});

// ── /approvals MU 接线：审批票列表直读 /api/mu/approvals（v16 审批门可见性）──
test('审批页（MU）：票列表+状态筛选+诚实空态+DRY_RUN 声明', async () => {
  ROUTES = {
    ...COMMON,
    '/api/health': () => [200, {
      service: 'console', data_mode: 'live',
      sources: { primary: 'multiuser', multiuser: { available: true } },
    }],
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'm1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ['read_pull_request', 'decide_review'], memberships: [],
    }],
    '/api/mu/approvals': (u) => {
      const st = new URL(u.url, 'http://x').searchParams.get('status');
      const all = [
        { approval_id: 'ap-1', run_id: 'r-1', finding_id: 'f-1', severity: 'P0', status: 'PENDING',
          head_sha: 'ab'.repeat(20), pr_number: 4242, repo_owner: 'acme', repo_name: 'app',
          rule_id: 'R-SECRET', path: 'a.js', line_start: 3, summary_masked: 'sk-***',
          created_at: '2026-10-02T10:00:00Z', expires_at: '2026-10-09T00:00:00Z',
          decided_by: null, decided_at: null, run_status: 'WAITING_FOR_HUMAN_APPROVAL' }];
      return [200, { approvals: st ? all.filter((t) => t.status === st) : all }];
    },
  };
  const { json, renderer } = await renderRoute('/approvals');
  const text = json();
  const visible = allText(renderer.toJSON());
  try {
    assert.ok(text.includes('待审批'), '页面标题');
    assert.ok(text.includes('R-SECRET') && text.includes('acme') && text.includes('4242'), '票行（仓库/PR/发现）');
    assert.ok(visible.includes('待审批') && visible.includes('危急'), '状态/级别列走 status-map 中文词表（PENDING→待审批、P0→危急）');
    assert.ok(!/\bPENDING\b/.test(visible) && !/\bSTALE\b/.test(visible) && !/\bCONSUMED\b/.test(visible),
      '可见文本无裸 enum（raw 值只允许在 title/技术详情）');
    assert.ok(text.includes('approval.tickets: PENDING'), 'raw enum 保留在 title 技术详情');
    assert.ok(text.includes('DRY_RUN 修复建议') && text.includes('不自动合并'), 'DRY_RUN 声明');
    assert.ok(text.includes('打开 PR'), '直达 PR 链接');
  } catch (e) { console.log(text.slice(0, 2500)); throw e; }
});
