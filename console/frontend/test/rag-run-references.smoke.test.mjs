// rag-run-references.smoke.test.mjs — 知识库参考（RAG · reference only）双源行为回归
// （#491 复核定案：源 A=审查执行期快照（历史权威）；源 B=当前知识库检索（实时不落库））。
//  R1（App 级集成锚）：双源区块同时上屏，快照带元数据（时间/分支/策略），引用可追溯；
//  R2（组件级 403 双源均越权）→ 不渲染；
//  R3（组件级 未记录 + 实时 empty）→ 「未记录」如实上屏（不补造历史）+ 实时如实 empty；
//  R4（组件级 实时未接线）→ 实时区隐藏、快照区仍如实「未记录」；
//  R5（组件级 快照 provider_unavailable）→ 「曾尝试但服务不可用」如实记录；
//  R6（防伪造）→ 页面不出现「快照已保存」类谎报（实时查询不落库语义上屏）。
// 运行：cd console/frontend && node --test test/rag-run-references.smoke.test.mjs（需先 npm ci）
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
const ACTIONS = ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query',
  'request_review', 'decide_review', 'apply_fix', 'merge_pr'];
const RUN = 'run-rag';
const HEAD = 'e'.repeat(40);

let ROUTES = {};
globalThis.fetch = async (input, init) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const out = route(u, init);
  if (out instanceof Promise) return out;
  const [status, body] = out;
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

let loadBundle = null;
function loadModule() {
  loadBundle ??= (async () => {
    const outDir = path.join(FRONTEND, 'node_modules', '.ragref-smoke');
    fs.mkdirSync(outDir, { recursive: true });
    const entry = path.join(outDir, 'entry.mjs');
    const bundle = path.join(outDir, 'bundle.cjs');
    fs.writeFileSync(entry, [
      `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};`,
      `import { clearRuntimeConfigCache } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/data/config.js')))};`,
      `import { RunKnowledgeReferences } from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/components/ReviewArtifactsPanel.jsx')))};`,
      'export { App, clearRuntimeConfigCache, RunKnowledgeReferences };',
    ].join('\n'));
    await build({
      entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
      jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
      define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
    });
    const mod = await import(pathToFileURL(bundle).href);
    return { App: mod.App ?? mod.default?.App ?? mod.default, clear: mod.clearRuntimeConfigCache,
      RunKnowledgeReferences: mod.RunKnowledgeReferences };
  })();
  return loadBundle;
}

async function renderRoute(route) {
  const loaded = await loadModule();
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
const waitFor = async (cond, deadlineMs = 4000) => {
  const t0 = Date.now();
  for (;;) {
    await flush();
    if (cond()) return true;
    if (Date.now() - t0 > deadlineMs) return false;
    await new Promise((r) => setTimeout(r, 50));
  }
};

function baseRoutes() {
  return {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, { service: 'console', data_mode: 'multiuser',
      sources: { primary: 'multiuser', multiuser: { available: true } }, mu_schema_ready: true }],
    '/api/mu/session': () => [200, { user: { user_id: 'u-1', login: 'smoke1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ACTIONS, memberships: [] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider: 'github', provider_repo_id: 'R_1' }] }],
    '/api/mu/prs/12': () => [200, {
      pull_request: { pr_id: 'pr-1', repo_id: 'r-1', provider: 'github', provider_pr_number: 12,
        head_sha: HEAD, head_ref: 'feature/docs', base_ref: 'main', branch_protection_status: 'unknown', title: 'fix' },
      review_records: [],
      latest_run: { run_id: RUN, status: 'COMPLETED', architecture_version: 'v2',
        review_mode: 'evidence_only', review_verdict: 'changes_requested',
        verification_verdict: null, tests_status: 'not_run', merge_eligibility: 'unknown' },
      my_permissions: { actions: ACTIONS } }],
    '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: [] }],
    '/api/mu/runs': () => [200, { runs: [
      { run_id: RUN, provider_pr_number: 12, status: 'COMPLETED', head_sha: HEAD,
        created_at: '2026-10-18T00:00:00Z', repo_id: 'r-1' }] }],
    '/api/mu/runs/run-rag/artifacts': () => [200, {
      run: { run_id: RUN, status: 'COMPLETED', head_sha: HEAD, created_at: '2026-10-18T00:00:00Z',
        owner: 'acme', repo_name: 'app', provider: 'github' },
      findings: [{ finding_id: 'f-1', rule_id: 'DEMO-NULL-BUG', severity: 'P1', path: 'discount.js',
        line_start: 6, line_end: null, title: 'null TypeError when tier missing（演示发现）',
        summary_masked: 'null TypeError', remediation: '', evidence_ref: '', source: 'precheck' }],
      outputs: {}, patch: null, feedback: [], publish: null,
      parse_state: { outputs_ok: 0, parse_failures: [] },
      verification: [{ kind: 'static_rules', label: '静态规则检查', executed: true }],
    }],
    '/api/mu/runs/run-rag/merge-pr/preview': () => [409, { error: { reason: 'remote_unreachable' } }],
    '/api/mu/runs/run-rag/apply-fix/preview': () => [409, { error: { reason: 'remote_unreachable' } }],
    '/api/mu/runs/run-rag/publish': () => [409, { error: { reason: 'remote_unreachable' } }],
    '/api/mu/prs/pr-1/repair': () => [409, { error: { reason: 'no_repair_pending' } }],
    '/api/mu/prs/pr-1/decision': () => [409, { error: { reason: 'no_pending' } }],
    '/api/mu/prs/pr-1/review': () => [200, { ok: true }],
    '/api/rag-trial/review-aux': (u, init) => {
      if (String(init?.method ?? 'GET').toUpperCase() !== 'POST') {
        return [405, { error: { reason: 'method_not_allowed' } }];
      }
      return reviewAuxResponse();
    },
    '/api/rag-trial/snapshot/run-rag': () => snapshotResponse(),
  };
}

const ROUTE = '/mu/repos/acme/app/pr/12';
after(() => { try { domWindow.close(); } catch { /* */ } setTimeout(() => process.exit(0), 300); });

const CITE = [{ kind: 'rag_auxiliary', run_id: RUN, usage: 'reference_only', trusted: false,
  citation: { repo: 'nghqqa/ragdemo', branch: 'main', doc_path: 'review-policy.md',
    line_start: 15, line_end: 28, para_index: 2, chunk_index: 3,
    doc_sha256: 'd'.repeat(64), chunk_sha256: 'c'.repeat(64),
    model_id: 'bge-m3', model_digest: 'm'.repeat(64), index_version: 1, corpus_branch: 'main' },
  score: 0.37, snippet: '维护者逐次「预览 → 确认」……（节选）',
  policy: { may_auto_promote: false, excluded_from: ['finding', 'ticket', 'gate', 'VERIFIED'] } }];

const snapshotHit = () => [200, {
  service_state: 'snapshot', run_id: RUN, corpus_branch: 'main', base_ref: 'main',
  review_head: HEAD, model_id: 'bge-m3', model_digest: 'm'.repeat(64), index_version: 1,
  query_strategy: 'pr_context_v1', snapshot_at: '2026-10-19T01:02:03.000Z', citations: CITE,
  note: '审查执行期获取的知识库引用快照',
}];
const hitBody = () => ({
  service_state: 'hit',
  run_view: { run_id: RUN,
    review_context: { rag_auxiliary: CITE,
      rag_policy_note: 'reference only — 不构成 finding/ticket/gate/VERIFIED 输入' } },
  policy: { may_auto_promote: false },
  queried_at: '2026-10-19T09:00:00.000Z',
  note: 'RAG 命中仅作 Review 辅助引用；本查询为实时对照，不落执行期快照',
});

let reviewAuxResponse = () => [200, hitBody()]; // 各用例覆写（模块级可变闭包）
let snapshotResponse = () => snapshotHit();

test('R1（App 级集成锚）双源：快照区+实时区同时上屏，快照带时间/分支/策略且引用可追溯', async () => {
  ROUTES = baseRoutes();
  reviewAuxResponse = () => [200, hitBody()];
  snapshotResponse = () => snapshotHit();
  const ui = await renderRoute(ROUTE);
  try {
    const ok1 = await waitFor(() => ui.text().includes('审查时保存的引用快照'), 6000);
    const t = ui.text();
    assert.ok(ok1, '源 A 快照区上屏');
    assert.ok(t.includes('当前知识库检索'), '源 B 实时区上屏');
    assert.ok(t.includes('历史执行证据') && t.includes('实时对照'), '双源定性区分上屏');
    assert.ok(t.includes('2026-10-19 01:02:03'), '快照时间可见');
    assert.ok(t.includes('语料分支 main') && t.includes('pr_context_v1'), '快照分支/策略可见');
    assert.ok(t.includes('review-policy.md') && t.includes('L15-28'), '引用可追溯（doc_path+行号）');
    assert.ok(!t.includes('快照已保存'), '不谎报「已保存」（实时查询不落库）');
    // 安全边界声明收敛为区块级一次（两个分节不再各自重复）
    const refIdx = t.indexOf('知识库参考（RAG');
    const block = t.slice(refIdx, refIdx + 1600);
    assert.equal((block.match(/不构成 finding/g) || []).length, 1, 'reference-only 声明区块级恰好一次');
    assert.ok(block.includes('不供 Fixer/Verifier 使用'), 'Fixer/Verifier 边界保留');
  } finally { await ui.cleanup(); }
});

// ── 组件级直渲染（确定性） ──
async function renderComp() {
  const loaded = await loadModule();
  const container = domWindow.document.createElement('div');
  domWindow.document.body.appendChild(container);
  let root = null;
  const findings = [{ title: 'null TypeError when tier missing（演示发现）' }];
  await act(async () => {
    root = createRoot(container);
    root.render(React.createElement(loaded.RunKnowledgeReferences, {
      runId: RUN, repoFull: 'nghqqa/ragdemo', baseBranch: 'main', findings,
    }));
  });
  return {
    container,
    text: () => container.textContent ?? '',
    cleanup: async () => { if (root) await act(async () => root.unmount()); container.remove(); },
  };
}

test('R2 组件级双源均 403：不渲染（功能不适用而非异常）', async () => {
  reviewAuxResponse = () => [403, { error: { reason: 'scope_not_allowed' } }];
  snapshotResponse = () => [403, { error: { reason: 'run_tenant_mismatch' } }];
  const ui = await renderComp();
  try {
    await flush();
    assert.ok(!ui.text().includes('知识库参考'), '403 时不渲染区块');
  } finally { await ui.cleanup(); }
});

test('R3 组件级 未记录 + 实时 empty：未记录如实上屏（不补造历史），实时区如实 empty', async () => {
  reviewAuxResponse = () => [200, { service_state: 'empty', results: [] }];
  snapshotResponse = () => [200, { service_state: 'no_snapshot', note: '未记录', run_id: RUN }];
  const ui = await renderComp();
  try {
    const ok1 = await waitFor(() => ui.text().includes('未记录'));
    assert.ok(ok1, '「未记录」上屏');
    const t = ui.text();
    assert.ok(t.includes('不使用当前检索补造历史'), '不补造历史语义上屏');
    assert.ok(t.includes('当前知识库无相关内容'), '实时区如实 empty');
    assert.ok(!t.includes('review-policy.md'), '快照未记录时无引用内容');
  } finally { await ui.cleanup(); }
});

test('R4 组件级 实时未接线：实时区隐藏、快照区仍如实「未记录」', async () => {
  reviewAuxResponse = () => [200, { service_state: 'backend_not_wired', note: '如实返回' }];
  snapshotResponse = () => [200, { service_state: 'no_snapshot', run_id: RUN }];
  const ui = await renderComp();
  try {
    const ok1 = await waitFor(() => ui.text().includes('未记录'));
    assert.ok(ok1, '快照区未记录如实可见');
    assert.ok(!ui.text().includes('实时对照'), '未接线时实时区隐藏');
  } finally { await ui.cleanup(); }
});

test('R5 组件级 快照 retrieval_state=provider_unavailable：曾尝试但不可用（如实，不伪装命中）', async () => {
  reviewAuxResponse = () => [403, { error: { reason: 'scope_not_allowed' } }];
  snapshotResponse = () => [200, { service_state: 'snapshot', retrieval_state: 'provider_unavailable',
    corpus_branch: 'main', model_id: 'bge-m3', index_version: 1, query_strategy: 'pr_context_v1',
    snapshot_at: '2026-10-19T01:02:03.000Z', review_head: HEAD, base_ref: null, citations: [] }];
  const ui = await renderComp();
  try {
    const ok1 = await waitFor(() => ui.text().includes('曾尝试获取'));
    assert.ok(ok1, '「曾尝试但不可用」如实上屏');
    const t = ui.text();
    assert.ok(!t.includes('review-policy.md'), '不可用态不渲染引用列表');
    assert.ok(!t.includes('当前知识库检索'), '403 实时区隐藏');
  } finally { await ui.cleanup(); }
});

test('R6 防伪造：任何状态下页面不出现「快照已保存」谎报（实时查询不落执行期快照）', async () => {
  reviewAuxResponse = () => [200, hitBody()];
  snapshotResponse = () => [200, { service_state: 'no_snapshot', run_id: RUN }];
  const ui = await renderComp();
  try {
    await waitFor(() => ui.text().includes('未记录'));
    const t = ui.text();
    assert.ok(!t.includes('快照已保存'), '无「已保存」谎报');
    assert.ok(t.includes('不落执行期快照') || t.includes('不使用当前检索补造历史'), '不落库语义上屏');
  } finally { await ui.cleanup(); }
});

test('R7 长预览默认收起（3 行截断）→ 展开全文 → 收起', async () => {
  ROUTES = baseRoutes();
  reviewAuxResponse = () => [200, { service_state: 'empty', results: [] }];
  const longSnippet = '长文本预览。'.repeat(60); // >160 触发展开控件
  snapshotResponse = () => [200, { service_state: 'snapshot', retrieval_state: 'hit',
    corpus_branch: 'main', model_id: 'bge-m3', index_version: 1, query_strategy: 'pr_context_v1',
    snapshot_at: '2026-10-20T00:00:00.000Z', review_head: HEAD, base_ref: 'main',
    citations: [{ kind: 'rag_auxiliary', usage: 'reference_only', trusted: false,
      citation: { repo: 'nghqqa/ragdemo', branch: 'main', doc_path: 'long-doc.md',
        line_start: 1, line_end: 40, doc_sha256: 'e'.repeat(64), chunk_sha256: 'f'.repeat(64),
        model_id: 'bge-m3', index_version: 1, corpus_branch: 'main' },
      score: 0.42, snippet: longSnippet }] }];
  const ui = await renderComp();
  try {
    const ok1 = await waitFor(() => ui.text().includes('long-doc.md'));
    assert.ok(ok1, '长引用上屏');
    const preview = ui.container.querySelector('[data-rag-preview]');
    assert.ok(preview, '预览容器存在');
    // happy-dom 的 style 对象对 vendor 前缀回读不稳定——断言原始 style 属性串
    const clamped = () => (preview.getAttribute('style') || '').includes('max-height');
    assert.ok(clamped(), '默认收起（max-height 内联生效；line-clamp 为浏览器渐进增强）');
    const btn = ui.container.querySelector('[data-testid="rag-ref-toggle"]');
    assert.ok(btn && btn.textContent.includes('展开全文'), '展开控件存在');
    btn.click();
    await flush();
    const preview2 = ui.container.querySelector('[data-rag-preview]');
    assert.ok(!((preview2.getAttribute('style') || '').includes('max-height')), '展开后无截断');
    assert.ok(ui.container.querySelector('[data-testid="rag-ref-toggle"]').textContent.includes('收起'), '按钮变收起');
    ui.container.querySelector('[data-testid="rag-ref-toggle"]').click();
    await flush();
    assert.ok((ui.container.querySelector('[data-rag-preview]').getAttribute('style') || '').includes('max-height'), '再点恢复截断');
  } finally { await ui.cleanup(); }
});

test('R8 追溯元数据收进次级信息行（不与正文重复铺开）+ XSS 纯文本安全', async () => {
  ROUTES = baseRoutes();
  reviewAuxResponse = () => [403, { error: { reason: 'x' } }];
  const xss = '<img src=x onerror=window.__pwned=1>';
  snapshotResponse = () => [200, { service_state: 'snapshot', retrieval_state: 'hit',
    corpus_branch: 'main', model_id: 'bge-m3', index_version: 7, query_strategy: 'pr_context_v1',
    snapshot_at: '2026-10-20T00:00:00.000Z', review_head: HEAD, base_ref: 'main',
    citations: [{ kind: 'rag_auxiliary', usage: 'reference_only', trusted: false,
      citation: { repo: 'nghqqa/ragdemo', branch: 'main', doc_path: 'meta-doc.md',
        line_start: 3, line_end: 9, doc_sha256: 'a'.repeat(64), chunk_sha256: 'b'.repeat(64),
        model_id: 'bge-m3', index_version: 7, corpus_branch: 'main' },
      score: 0.5, snippet: xss }] }];
  const ui = await renderComp();
  try {
    await waitFor(() => ui.text().includes('meta-doc.md'));
    const t = ui.text();
    assert.ok(t.includes('doc aaaaaaaa…') && t.includes('chunk bbbbbbbb…'), 'doc/chunk sha 次级行可见');
    assert.ok(t.includes('bge-m3 @iv7'), '模型@索引版本可见');
    assert.ok(t.includes('语料分支 main'), '语料分支可见');
    assert.ok(t.includes(xss), 'snippet 按纯文本渲染（字面呈现）');
    assert.equal(ui.container.querySelector('img'), null, 'HTML 未被解析为元素（无注入）');
    const prev = ui.container.querySelector('[data-rag-preview]');
    assert.equal(prev.style.overflowWrap, 'anywhere', '长 token 自然换行约束在元素样式上');
  } finally { await ui.cleanup(); }
});
