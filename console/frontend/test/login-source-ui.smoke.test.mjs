// login-source-ui.smoke.test.mjs — 登录来源波前端行为冒烟。
// 锁六件事：
//  ① identity.js 纯函数契约：login_type 驱动标签；缺失/未知 → 来源未知（绝不冒充 GitHub）；
//  ② 进行中状态平台区分：仅被点击入口显示「正在前往 …」，另一入口禁用保持原文；
//  ③ 重入守卫：进行中连点不产生第二个 start 请求；
//  ④ 失败复位：错误后 pending 清零，按钮恢复可点（可重试）；
//  ⑤ 邀请错配 409 无 invite_provider 字段时退化为中性指引（不写死平台名）；
//  ⑥ 面向用户文案：页面不出现部署变量名（MU_*）/内部术语（auth_unavailable）；
//     stale「未配置」错误在 providers 就绪后不再复现（邀请上下文保留）。
// 复用 pages-smoke 基建（bundle 名独立，防模块缓存污染）。
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
import { deriveIdentitySource } from '../src/identity.js';

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

let ROUTES = {};
let calls = [];
let hangStart = false; // 置 true 时 start 请求挂起（模拟进行中）
let releaseStart = null;
globalThis.fetch = async (input) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  calls.push(u.pathname + u.search);
  const route = ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  if (hangStart && u.pathname.includes('/start')) {
    await new Promise((resolve) => { releaseStart = resolve; });
  }
  const [status, body] = route(u);
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

let LOAD_SEQ = 0;
async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.login-source-smoke');
  fs.mkdirSync(outDir, { recursive: true });
  const seq = ++LOAD_SEQ;
  const entry = path.join(outDir, `entry-${seq}.mjs`);
  const bundle = path.join(outDir, `bundle-${seq}.cjs`);
  fs.writeFileSync(entry, `import App from ${JSON.stringify(toFwd(path.join(FRONTEND, 'src/App.jsx')))};\nexport { App };`);
  await build({
    entryPoints: [entry], bundle: true, format: 'cjs', platform: 'node', outfile: bundle,
    jsx: 'automatic', external: ['react', 'react-dom', 'scheduler', 'react-router-dom'],
    define: { 'process.env.NODE_ENV': '"test"' }, logLevel: 'silent',
  });
  const mod = await import(pathToFileURL(bundle).href);
  return mod.App ?? mod.default?.App ?? mod.default ?? mod;
}

const HEALTH = () => [200, { service: 'console', data_mode: 'multiuser',
  sources: { primary: 'multiuser', multiuser: { available: true } }, mu_schema_ready: true }];
const ANON = () => [401, { error: { reason: 'not_authenticated' }, capabilities: { legacy_login: false, multiuser: true } }];
const BOTH_PROVIDERS = () => [200, {
  github: { configured: true, callback_url: 'http://x/cb', scope: 'read:user' },
  gitee: { configured: true, callback_url: 'http://x/cb-gitee', scope: 'user_info' },
}];

async function renderRoute(route) {
  const App = await loadApp();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); });
  return { renderer, json: () => JSON.stringify(renderer.toJSON()) };
}
const flush = async () => { for (let i = 0; i < 20; i++) await act(async () => { await Promise.resolve(); }); };
const label = (n) => String(Array.isArray(n.props?.children) ? n.props.children.join('') : (n.props?.children ?? ''));
const findBtn = (renderer, re) => renderer.root.find((n) => typeof n.props?.onClick === 'function' && re.test(label(n)));

after(() => {
  try { domWindow.close(); } catch { /* ignore */ }
  setTimeout(() => process.exit(0), 300);
});

// ── ① identity.js 纯函数契约 ──
test('LSV-1a identity: github-oauth → GitHub 标签；gitee-oauth → Gitee 标签', () => {
  const gh = deriveIdentitySource({ session: { user: { login_type: 'github-oauth' } }, authed: true });
  const ge = deriveIdentitySource({ session: { user: { login_type: 'gitee-oauth' } }, authed: true });
  assert.equal(gh.key, 'github_oauth');
  assert.equal(ge.key, 'gitee_oauth');
});
test('LSV-1b identity: 已登录但无 login_type → 来源未知（绝不冒充 GitHub；mu_session 兜底已删除）', () => {
  const unk = deriveIdentitySource({ session: { user: {}, session_source: 'mu_session' }, authed: true });
  const unk2 = deriveIdentitySource({ session: { user: { login_type: 'future-oauth' } }, authed: true });
  assert.equal(unk.key, 'unknown');
  assert.ok(!/GitHub/.test(unk.label), '未知来源标签不含 GitHub');
  assert.equal(unk2.key, 'unknown');
});
test('LSV-1c identity: operator_password → 操作员（行为保留）', () => {
  const op = deriveIdentitySource({ session: { user: { login_type: 'operator_password' } }, authed: true });
  assert.equal(op.key, 'operator_password');
});

// ── ②③ 进行中状态区分 + 重入守卫 ──
test('LSV-2 点击 Gitee → 仅 Gitee 显示「正在前往 Gitee…」，GitHub 禁用保持原文；连点仅一次 start', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    '/api/auth/session': ANON, '/api/health': HEALTH,
    '/api/mu/auth/providers': BOTH_PROVIDERS,
    '/api/mu/auth/oauth/gitee/start': () => [200, { authorize_url: 'https://gitee.com/oauth/authorize?state=x' }],
  };
  calls = []; hangStart = true; releaseStart = null;
  const { renderer } = await renderRoute('/login');
  try {
    const giteeBtn = findBtn(renderer, /使用 Gitee 登录/);
    await act(async () => { giteeBtn.props.onClick(); });
    await act(async () => { giteeBtn.props.onClick(); }); // 同 tick 连点
    await flush();
    assert.ok(calls.filter((c) => c === '/api/mu/auth/oauth/gitee/start').length === 1,
      `重入守卫：start 恰一次（实际 ${calls.join('|')}）`);
    const text = JSON.stringify(renderer.toJSON());
    assert.ok(text.includes('正在前往 Gitee…'), '被点击入口显示进行中文案');
    assert.ok(!text.includes('正在前往 GitHub'), '另一入口不显示进行中');
    assert.ok(text.includes('使用 GitHub 登录'), '另一入口保持原文');
    const ghBtn = renderer.root.find((n) => n.props?.disabled === true && /使用 GitHub 登录/.test(label(n)));
    assert.ok(ghBtn, '另一入口禁用但可读');
    const giteePending = renderer.root.find((n) => /正在前往 Gitee…/.test(label(n)));
    assert.equal(giteePending.props.disabled, true, '进行中入口自身禁用（防重复）');
  } finally {
    releaseStart?.(); hangStart = false;
    await act(async () => { renderer.unmount(); });
  }
});

// ── ④ 失败复位可重试 ──
test('LSV-3 start 失败 → pending 复位，按钮恢复可点（可重试）', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    '/api/auth/session': ANON, '/api/health': HEALTH,
    '/api/mu/auth/providers': BOTH_PROVIDERS,
    '/api/mu/auth/oauth/github/start': () => [500, { error: { reason: 'boom' } }],
  };
  calls = [];
  const { renderer } = await renderRoute('/login');
  try {
    const ghBtn = findBtn(renderer, /使用 GitHub 登录/);
    await act(async () => { ghBtn.props.onClick(); });
    await flush();
    const text = JSON.stringify(renderer.toJSON());
    console.log('DBG calls=', JSON.stringify(calls), '| hasUnable=', text.includes('无法发起'), '| hasPending=', text.includes('正在前往'), '| hasUseGH=', text.includes('使用 GitHub 登录'), '| hasNetErr=', text.includes('网络错误')); assert.ok(text.includes('无法发起 GitHub'), '失败人话提示上屏');
    assert.ok(!text.includes('正在前往 GitHub…'), '失败后进行中文案清除');
    const retryBtn = findBtn(renderer, /使用 GitHub 登录/);
    assert.equal(retryBtn.props.disabled, false, '失败后按钮恢复可点（重试路径）');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// ── ⑤ 错配 409 无 invite_provider → 中性指引（不写死平台名） ──
test('LSV-4 错配 409 缺 invite_provider 字段 → 中性指引，不写死平台名', async () => {
  domWindow.location.href = 'http://smoke.local/login?invite=22222222-2222-4222-8222-222222222222';
  ROUTES = {
    '/api/auth/session': ANON, '/api/health': HEALTH,
    '/api/mu/auth/providers': BOTH_PROVIDERS,
    '/api/mu/auth/oauth/gitee/start': () => [409, { error: { reason: 'invite_provider_mismatch' } }],
  };
  const { renderer, json } = await renderRoute('/login?invite=22222222-2222-4222-8222-222222222222');
  try {
    const btn = findBtn(renderer, /使用受邀的 Gitee 账号登录/);
    await act(async () => { btn.props.onClick(); });
    await flush();
    const text = json();
    assert.ok(text.includes('该邀请绑定的平台与所选入口不一致'), '中性指引上屏');
    assert.ok(!text.includes('该邀请绑定的是 GitHub'), '不写死 GitHub');
    assert.ok(!text.includes('该邀请绑定的是 Gitee 账号——'), '不写死 Gitee');
  } finally { await act(async () => { renderer.unmount(); }); }
});

// ── ⑥ 面向用户文案 + stale 错误清理保留邀请上下文 ──
test('LSV-5 登录页文案不含部署变量名/内部术语；未配置平台不展示入口且提示人话', async () => {
  domWindow.location.href = 'http://smoke.local/login';
  ROUTES = {
    '/api/auth/session': ANON, '/api/health': HEALTH,
    '/api/mu/auth/providers': () => [200, {
      github: { configured: false, callback_url: null, reason: 'oauth_not_configured', scope: 'read:user' },
      gitee: { configured: true, callback_url: 'http://x/cb-gitee', scope: 'user_info' },
    }],
  };
  const { renderer, json } = await renderRoute('/login');
  try {
    const text = json();
    assert.ok(!text.includes('MU_'), '不出现部署变量名前缀');
    assert.ok(!text.includes('auth_unavailable'), '不出现内部 reason 术语');
    assert.ok(!text.includes('使用 GitHub 登录'), '未配置平台不渲染入口');
    assert.ok(text.includes('GitHub 登录入口未开放——需管理员在服务端配置 GitHub OAuth'), '未配置平台如实提示（人话，含启用方式）');
    assert.ok(text.includes('使用 Gitee 登录'), '已配置平台正常渲染');
  } finally { await act(async () => { renderer.unmount(); }); }
});

test('LSV-6 stale「未配置」错误在 providers 就绪后不复现；邀请上下文保留', async () => {
  domWindow.location.href = 'http://smoke.local/login?invite=33333333-3333-4333-8333-333333333333&mu_login_error=oauth_not_configured';
  ROUTES = {
    '/api/auth/session': ANON, '/api/health': HEALTH,
    '/api/mu/auth/providers': BOTH_PROVIDERS,
    '/api/mu/auth/oauth/gitee/start': () => [200, { authorize_url: 'https://gitee.com/oauth/authorize?state=y' }],
  };
  calls = [];
  const { renderer, json } = await renderRoute('/login?invite=33333333-3333-4333-8333-333333333333&mu_login_error=oauth_not_configured');
  try {
    const text = json();
    assert.ok(!text.includes('该登录方式尚未开放'), 'stale 未配置错误已清理');
    assert.ok(text.includes('检测到受邀访问'), '邀请上下文保留');
    const btn = findBtn(renderer, /使用受邀的 Gitee 账号登录/);
    await act(async () => { btn.props.onClick(); });
    await flush();
    assert.ok(calls.some((c) => c === '/api/mu/auth/oauth/gitee/start?invite=33333333-3333-4333-8333-333333333333'),
      '清理 stale 错误后 invite 参数仍随 start 传递');
  } finally { await act(async () => { renderer.unmount(); }); }
});
