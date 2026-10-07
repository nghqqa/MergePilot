// mu-pr-detail-slim.smoke.test.mjs — PR 详情信息精简回归锁（2026-10-07）。
// 三层结构：摘要（状态/阻塞/计数/动作）→ 审批+发现 → 技术详情折叠。
// 覆盖场景（契约一致脱敏夹具）：
//   1) 有发现待审批（WAITING）：顶部状态/阻塞一句/待审批计数；旧重复块移除；
//      技术详情默认折叠（四域机制说明不上屏）
//   2) 保护未知：合并入口唯一（合并资格：未知 + 操作路径卡片），旧重复标题移除
//   3) 保护已知：无阻塞条
//   4) 无发现：未发现风险项 + 审批区不出现
//   5) 权限不足：可见文字提示（非仅 hover tooltip）
//   6) 加载失败：读取失败 + 重试
// 运行：cd console/frontend && node --test test/mu-pr-detail-slim.smoke.test.mjs（需先 npm ci）
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
  const outDir = path.join(FRONTEND, 'node_modules', '.mu-slim-smoke');
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
  return mod.App ?? mod.default?.App ?? mod.default;
}

const flush = async () => { for (let i = 0; i < 30; i++) await act(async () => { await Promise.resolve(); }); };
const waitText = async (ui, probe, rounds = 60) => {
  for (let i = 0; i < rounds; i++) {
    if (ui.text().includes(probe)) return true;
    await act(async () => { await Promise.resolve(); });
  }
  return ui.text().includes(probe);
};
const countOf = (text, probe) => text.split(probe).length - 1;

// ── 场景夹具（契约一致：字段全部来自 /api/mu/* 真实形状）──
const FINDINGS = [
  { finding_id: 'f-1', rule_id: 'R-SECRET', severity: 'P0', path: 'sample_utils.py', line_start: 17,
    summary_masked: 'DEFAULT_PASSWORD = "dev-demo-password-2026"',
    evidence_ref: 'diff:sample_utils.py#L17', remediation: '凭据移入 secret manager/环境变量' },
  { finding_id: 'f-2', rule_id: 'R-SQL-CONCAT', severity: 'P1', path: 'sample_utils.py', line_start: 22,
    summary_masked: 'sql = "SELECT * FROM users WHERE id = " + str(user_id)',
    evidence_ref: 'diff:sample_utils.py#L22', remediation: '改用参数化查询/预编译语句' },
];
const TICKETS = [
  { approval_id: 'ap-1', severity: 'P0', rule_id: 'R-SECRET', path: 'sample_utils.py', line_start: 17,
    summary_masked: 'DEFAULT_PASSWORD = "dev-demo-password-2026"',
    head_sha: 'ab'.repeat(20), status: 'PENDING', expires_at: '2026-10-14T13:45:49Z' },
  { approval_id: 'ap-2', severity: 'P1', rule_id: 'R-SQL-CONCAT', path: 'sample_utils.py', line_start: 22,
    summary_masked: 'sql = "SELECT * FROM users WHERE id = " + str(user_id)',
    head_sha: 'ab'.repeat(20), status: 'PENDING', expires_at: '2026-10-14T13:45:49Z' },
];

/**
 * buildRoutes({ actions, runStatus, findings, tickets, protection, detailFail, withLlm })
 * 默认：WAITING + 2 发现 + 2 待审批票 + 保护未知。
 */
function buildRoutes({ actions = ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query',
  'request_review', 'decide_review', 'request_repair', 'manage_repository_binding'],
  runStatus = 'WAITING_FOR_HUMAN_APPROVAL', findings = FINDINGS, tickets = TICKETS,
  protection = 'unknown', detailFail = false, withLlm = true,
  reviewVerdict, latestRun = {} } = {}) {
  const AB = 'ab'.repeat(20);
  const verdict = reviewVerdict ?? (findings.length ? 'findings_present' : 'no_blocking_findings');
  return {
    '/api/auth/session': () => [200, SESSION],
    '/api/health': () => [200, { service: 'console', data_mode: 'fixture',
      sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } } }],
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-1', login: 'dana' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: actions.includes('decide_review') ? 'maintainer' : 'reviewer',
      actions, memberships: [] }],
    '/api/mu/github/app/status': () => [200, { configured: true }],
    '/api/mu/github/installations': () => [200, { installations: [{ installation_id: 1, revoked: null, suspended: null }] }],
    '/api/mu/repositories': () => [200, { repositories: [
      { repo_id: 'r-1', owner: 'acme', name: 'app', provider_repo_id: 'R_gh_9001',
        binding_id: 'b-1', binding_kind: 'github_app', installation_state: 'active' }] }],
    '/api/mu/invitations': () => [200, { invitations: [] }],
    '/api/mu/members': () => [200, { members: [] }],
    '/api/mu/prs': () => [200, { pull_requests: [
      { pr_id: 'pr-1', provider_pr_number: 11, head_sha: AB,
        branch_protection_status: protection, state: 'open', updated_at: '2026-10-07T13:45:49Z' }] }],
    '/api/mu/prs/11': () => detailFail ? [500, { error: { reason: 'internal' } }] : [200, {
      pull_request: { pr_id: 'pr-1', provider_pr_number: 11, head_sha: AB,
        branch_protection_status: protection, title: '精简验收（脱敏假名）', state: 'open' },
      latest_run: { run_id: 'run-1', status: runStatus, review_verdict: verdict,
        verification_verdict: 'not_run', tests_status: 'not_run', merge_eligibility: 'unknown',
        review_mode: 'local', architecture_version: 'v2', ...latestRun },
      review_records: [],
      my_permissions: { actions } }],
    '/api/mu/prs/pr-1/fix-approvals': () => [200, { fix_approvals: tickets }],
    '/api/mu/runs': () => [200, { runs: [{ run_id: 'run-1', provider_pr_number: 11 }] }],
    '/api/mu/runs/run-1': () => [200, { run: { run_id: 'run-1', status: runStatus,
      provider_pr_number: 11, head_sha: AB, policy_version: 1,
      ...(withLlm ? { llm_mode: 'deterministic_only' } : {}) },
      attempts: [{ agent_role: 'reviewer', attempt: 1, status: 'DONE', provider: 'deterministic',
        latency_ms: 3100, created_at: '2026-10-07T13:45:46Z', error_code: null }],
      findings, fixes: [], verifications: [], decisions: [], dead_letters: [] }],
  };
}

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
  const clickPrRow = async () => {
    const row = [...container.querySelectorAll('.mu-pr-row')].find((x) => x.textContent.includes('#11'));
    assert.ok(row, 'PR #11 行已渲染');
    await act(async () => { row.click(); });
    await flush();
  };
  return {
    text: () => container.textContent ?? '',
    clickPrRow,
    waitText: (probe) => waitText({ text: () => container.textContent ?? '' }, probe),
    permHint: () => container.querySelector('.mu-detail-permhint')?.textContent ?? null,
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

// ── 场景 1：有发现待审批（WAITING，默认全量夹具）──
test('有发现待审批：顶部状态/阻塞/计数集中，技术详情默认折叠', async () => {
  ROUTES = buildRoutes();
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await ui.waitText('执行器：内置规则引擎'), '管线面板就绪');
    const text = ui.text();
    assert.ok(text.includes('待人工批准（高危修复）'), 'run 状态中文标签上屏');
    assert.ok(text.includes('待审批 2 张（P0/P1）'), '待审批计数上屏');
    assert.ok(text.includes('修复已暂停——等待全部 P0/P1 票人工批准'), '阻塞原因一句话上屏');
    assert.ok(text.includes('批准仅启动 dry-run 修复建议——不写入 GitHub、不自动合并'), '审批旁短说明保留');
    assert.ok(!text.includes('Fixer 被阻塞'), '旧重复阻塞横幅已并入顶部摘要');
    assert.ok(!text.includes('保护状态未知（合并资格 fail-closed）'), '旧重复保护标题已移除');
    assert.ok(!text.includes('四项独立判定互不冒充'), '机制长说明默认折叠不上屏');
    assert.ok(!text.includes('修复建议仍需人工复核后才可能被应用'), '长边界段落默认折叠不上屏');
    assert.ok(text.includes('未发现风险项') === false || findingsPresentGuard(text), '发现内容按夹具呈现');
  } finally { await ui.cleanup(); }
});
const findingsPresentGuard = (text) => text.includes('风险项（Reviewer 发现）');

// ── 场景 2：保护未知——合并入口唯一 ──
test('保护未知：合并资格单入口，一句原因+操作路径', async () => {
  ROUTES = buildRoutes();
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await ui.waitText('合并资格：'), '合并资格行上屏');
    const text = ui.text();
    assert.ok(text.includes('合并资格：未知——分支保护状态未知'), '单入口一句原因上屏');
    assert.ok(text.includes('可能原因'), '操作路径卡片渲染（修正历史传参错位后生效）');
    assert.ok(text.includes('刷新状态') || text.includes('仓库设置') || text.includes('配置指南'), '候选原因带操作路径');
  } finally { await ui.cleanup(); }
});

// ── 场景 3：保护已知——无阻塞条 ──
test('保护已知：不出现合并资格未知阻塞条', async () => {
  ROUTES = buildRoutes({ protection: 'known_clean', runStatus: 'COMPLETED', findings: [], tickets: [] });
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await ui.waitText('未发现风险项'), '无发现空态上屏');
    const text = ui.text();
    assert.ok(!text.includes('合并资格：未知——分支保护状态未知'), '无未知阻塞条');
    assert.ok(!text.includes('高危修复审批（P0/P1 逐条）'), '无票且非 WAITING 时审批区不出现');
  } finally { await ui.cleanup(); }
});

// ── 场景 4：权限不足——可见提示（非仅 hover）──
test('权限不足：可见文字提示角色要求', async () => {
  ROUTES = buildRoutes({ actions: ['read_repository', 'read_pull_request', 'read_code_content', 'rag_query', 'request_review'] });
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await ui.waitText('执行器：内置规则引擎'), '管线面板就绪');
    const hint = ui.permHint();
    assert.ok(hint, '权限提示以可见元素呈现');
    assert.ok(hint.includes('maintainer'), '提示含所需角色');
    assert.ok(hint.includes('已禁用'), '提示说明按钮禁用语义');
  } finally { await ui.cleanup(); }
});

// ── 场景 5：加载失败——读取失败 + 重试 ──
test('加载失败：读取失败提示 + 重试入口', async () => {
  ROUTES = buildRoutes({ detailFail: true });
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await ui.waitText('读取失败'), '失败提示上屏');
    assert.ok(ui.text().includes('重试'), '重试入口在');
  } finally { await ui.cleanup(); }
});

// ── 场景 6：策略缺失——档位不猜测（#364 语义在新结构下保持）──
test('策略缺失：中性文案无档位编造', async () => {
  ROUTES = buildRoutes({ withLlm: false });
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await ui.waitText('内置规则引擎（确定性审查）'), '中性引擎文案');
    assert.ok(!ui.text().includes('档位快照'), '缺失字段无档位子句');
  } finally { await ui.cleanup(); }
});

// ── 场景 7：四域判定折叠区展开后内容上屏（承接 mu-review-arch 旧默认可见锁）──
test('四域判定展开：中文标签+枚举原值+机制说明上屏', async () => {
  ROUTES = buildRoutes({
    runStatus: 'VERIFYING', reviewVerdict: 'changes_requested',
    latestRun: { verification_verdict: 'inconclusive', tests_status: 'passed',
      merge_eligibility: 'ineligible', code_egress: 2,
      model_judgment: { verdict: 'PASS', input: 'digest_only', note: '仅审计留痕' },
      test_evidence: 'tools:static_check=ok,secret_scan=ok' },
    findings: [{ ...FINDINGS[0] }],
    tickets: [TICKETS[0]],
  });
  const ui = await renderMultiUser();
  try {
    await ui.clickPrRow();
    assert.ok(await ui.waitText('技术详情与判定'), '折叠区入口上屏');
    assert.ok(!ui.text().includes('四项独立判定互不冒充'), '默认折叠不上屏');
    await act(async () => {
      const header = container().querySelector('.mu-tech-collapse .ant-collapse-header');
      assert.ok(header, '折叠区头可点');
      header.click();
    });
    await flush();
    const text = ui.text();
    assert.ok(text.includes('审查结论') && text.includes('要求修改'), '审查结论中文标签+枚举值上屏');
    assert.ok(text.includes('验证结论（模型域）') && text.includes('不确定'), '模型域分显');
    assert.ok(text.includes('测试证据（工具域）'), '工具域分显');
    assert.ok(text.includes('模型判定（原始）'), '模型判定分显');
    assert.ok(text.includes('次调用（审计在案）'), '出站计数上屏');
    assert.ok(text.includes('模型未读取完整 patch'), '机制说明展开后上屏');
  } finally { await ui.cleanup(); }
});
function container() { return domWindow.document.body; }
