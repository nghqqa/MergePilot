// rag-run-references.smoke.test.mjs — 知识库参考（RAG · reference only）行为回归
// （2026-10-18 审查引用接线；独立审阅要求组件级回归）。
//  R1（App 级集成锚）：真实渲染 PR 详情页——命中态「知识库参考」区块出现，
//     引用可追溯（doc_path/行号/chunk+doc sha/index_version）并明确仅供参考；
//  R2/R3/R4（组件级直渲染 RunKnowledgeReferences，确定性）：
//     403 越权→隐藏；empty→如实「知识库无相关内容」；backend_not_wired→隐藏。
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

function baseRoutes({ reviewAux }) {
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
  };
}

const ROUTE = '/mu/repos/acme/app/pr/12';
after(() => { try { domWindow.close(); } catch { /* */ } setTimeout(() => process.exit(0), 300); });

const hitBody = () => ({
  service_state: 'hit',
  run_view: { run_id: RUN,
    review_context: {
      rag_auxiliary: [{ kind: 'rag_auxiliary', run_id: RUN, usage: 'reference_only', trusted: false,
        citation: { repo: 'nghqqa/ragdemo', branch: 'main', doc_path: 'review-policy.md',
          line_start: 15, line_end: 28, para_index: 2, chunk_index: 3,
          doc_sha256: 'd'.repeat(64), chunk_sha256: 'c'.repeat(64),
          model_id: 'bge-m3', model_digest: 'm'.repeat(64), index_version: 1 },
        score: 0.37, snippet: '维护者逐次「预览 → 确认」……（节选）',
        policy: { may_auto_promote: false, excluded_from: ['finding', 'ticket', 'gate', 'VERIFIED'] } }],
      rag_policy_note: 'reference only — 不构成 finding/ticket/gate/VERIFIED 输入' } },
  policy: { may_auto_promote: false },
  note: 'RAG 命中仅作 Review 辅助引用',
});

let reviewAuxResponse = () => [200, hitBody()]; // 各用例覆写（模块级可变闭包）

test('R1（App 级集成锚）命中：run 页出现「知识库参考」，引用可追溯并明确仅供参考', async () => {
  ROUTES = baseRoutes(() => [200, hitBody()]);
  reviewAuxResponse = () => [200, hitBody()];
  const ui = await renderRoute(ROUTE);
  try {
    const ok1 = await waitFor(() => ui.text().includes('DEMO-NULL-BUG'));
    console.log('[dbg-R1] findings loaded:', ok1, '| TEXT:', JSON.stringify(ui.text().slice(0, 600)));
    const ok2 = await waitFor(() => ui.text().includes('知识库参考'), 6000);
    console.log('[dbg-R1] section:', ok2, '| TEXT tail:', JSON.stringify(ui.text().slice(-300)));
    assert.ok(ok2, '知识库参考区块出现');
    const t = ui.text();
    assert.ok(t.includes('review-policy.md'), '引用 doc_path 可见');
    assert.ok(t.includes('L15-28'), '行号可见（可追溯）');
    assert.ok(t.includes('仅供参考') && t.includes('不构成'), 'reference-only 定性上屏');
  } finally { await ui.cleanup(); }
});

// ── 组件级直渲染（确定性）：RunKnowledgeReferences 三态 ──
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
    text: () => container.textContent ?? '',
    cleanup: async () => { if (root) await act(async () => root.unmount()); container.remove(); },
  };
}

test('R2 组件级 403：不渲染（scope 未含该仓库——功能不适用）', async () => {
  reviewAuxResponse = () => [403, { error: { reason: 'scope_not_allowed' } }];
  const ui = await renderComp();
  try {
    await flush();
    assert.ok(!ui.text().includes('知识库参考'), '403 时不渲染区块');
  } finally { await ui.cleanup(); }
});

test('R3 组件级 empty：如实显示「知识库无相关内容」', async () => {
  reviewAuxResponse = () => [200, { service_state: 'empty', results: [] }];
  const ui = await renderComp();
  try {
    await flush();
    assert.ok(ui.text().includes('知识库无相关内容'), 'empty 如实上屏');
  } finally { await ui.cleanup(); }
});

test('R4 组件级未接线：backend_not_wired → 不渲染', async () => {
  reviewAuxResponse = () => [200, { service_state: 'backend_not_wired', note: '如实返回' }];
  const ui = await renderComp();
  try {
    await flush();
    assert.ok(!ui.text().includes('知识库参考'), '未接线时不渲染');
  } finally { await ui.cleanup(); }
});
