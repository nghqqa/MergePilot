// skill-page.smoke.test.mjs — B 波技能治理页冒烟 + PR-9 整改回归：
// /skills 真实 API 接线（列表/只读角色/诚实空态/错误态/区分声明/无 fixture 演练数据）；
// 五动作 busy 防重复（双击一次 POST）、错误 reason 机器码→中文映射、历史加载失败可重试、
// 发布响应 current_version 真值展示、aria-live 结果区、三句话边界文案、停用 tooltip。
// 文案/结构断言走 react-test-renderer；弹层交互（Modal/Popconfirm）走真实 react-dom+happy-dom
// （antd Dialog 的 portal 在 RTR 下不落地，见 tmp 注记）。
import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import React, { act } from 'react';
import TestRenderer from 'react-test-renderer';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { Window } from 'happy-dom';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(__dirname, '..');
const toFwd = (p) => p.replace(/\\/g, '/');

const domWindow = new Window();
if (!globalThis.window) globalThis.window = domWindow;
for (const k of ['HTMLElement', 'SVGElement', 'ShadowRoot', 'Element', 'Node', 'Document',
  'MouseEvent', 'KeyboardEvent', 'Event', 'CustomEvent', 'DOMRect', 'ResizeObserver', 'MutationObserver',
  'HTMLBodyElement', 'HTMLHtmlElement', 'HTMLInputElement', 'HTMLTextAreaElement', 'HTMLAnchorElement',
  'HTMLButtonElement', 'HTMLSpanElement', 'HTMLDivElement', 'Text', 'Comment', 'DocumentFragment']) {
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
let CALLS = []; // 每次真实 fetch 记一条 `${METHOD} ${pathname}`
const countCalls = (method, p) => CALLS.filter((c) => c === `${method} ${p}`).length;
globalThis.fetch = async (input, init = {}) => {
  const u = new URL(typeof input === 'string' ? input : input.url, 'http://smoke.local');
  const method = String(init.method || 'GET').toUpperCase();
  CALLS.push(`${method} ${u.pathname}`);
  const route = ROUTES[`${method} ${u.pathname}`] ?? ROUTES[u.pathname];
  if (!route) return new Response(JSON.stringify({ error: { reason: `unexpected_path:${u.pathname}` } }), { status: 404 });
  const [status, body] = await route(u); // route 可返回 Promise（hold 住在飞请求测防重复）
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
};

async function loadApp() {
  const outDir = path.join(FRONTEND, 'node_modules', '.skill-smoke');
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
  ...SESSION,
  '/api/auth/session': () => [200, SESSION],
  '/api/health': () => [200, {
    service: 'console', data_mode: 'live',
    sources: { primary: 'multiuser', multiuser: { available: true } },
  }],
};

const ADMIN_SESSION = {
  '/api/mu/session': () => [200, {
    user: { user_id: 'u-1', login: 'admin1' },
    tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
    role: 'platform_admin', actions: ['read_repository', 'manage_instance'], memberships: [],
  }],
};

// ── 渲染 A：react-test-renderer（文案/结构断言，无弹层）──
async function render(route) {
  const loaded = await loadApp();
  const App = loaded.App;
  if (loaded.clear) loaded.clear();
  let renderer;
  await act(async () => {
    renderer = TestRenderer.create(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 60)); });
  const json = () => JSON.stringify(renderer.toJSON());
  return { json, root: renderer.root, unmount: () => renderer.unmount() };
}

// ── 渲染 B：真实 react-dom + happy-dom（Modal/Popconfirm 弹层交互）──
// 注：happy-dom 下 React 18 的事件委托对 portal（document.body）内容不生效，
// 因此交互统一经 DOM 节点上的 __reactProps$ 直接调用 React 回调（等价于真实 onClick/onChange）。
async function renderDom(route) {
  const loaded = await loadApp();
  const App = loaded.App;
  if (loaded.clear) loaded.clear();
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = ''; // 测试间完全隔离（前一用例的弹层/表格残留会污染 querySelector）
  const host = document.createElement('div');
  document.body.appendChild(host);
  let handle;
  await act(async () => {
    handle = createRoot(host);
    handle.render(React.createElement(MemoryRouter, { initialEntries: [route] }, React.createElement(App)));
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 80)); });
  const close = async () => {
    await act(async () => { handle.unmount(); });
    host.remove();
    document.body.innerHTML = '';
  };
  return { close };
}

const settle = (ms = 50) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });
const norm = (s) => String(s ?? '').replace(/\s+/g, '');
const qAll = (sel) => [...document.querySelectorAll(sel)];
const reactProps = (el) => {
  const k = Object.keys(el ?? {}).find((x) => x.startsWith('__reactProps$'));
  return k ? el[k] : null;
};
const domBtn = (text) => qAll('button').find((b) => norm(b.textContent) === norm(text)) ?? null;
const domClick = async (el) => {
  assert.ok(el, '目标 DOM 元素存在');
  const p = reactProps(el);
  assert.ok(p && typeof p.onClick === 'function', `React onClick 可达（${el.textContent?.slice(0, 12)}）`);
  await act(async () => { p.onClick({ preventDefault() {}, stopPropagation() {}, persist() {}, target: el }); });
  await settle();
};
const domSetInput = async (selector, value) => {
  const el = document.querySelector(selector);
  assert.ok(el, `输入框存在：${selector}`);
  const p = reactProps(el);
  assert.ok(p && typeof p.onChange === 'function', `React onChange 可达（${selector}）`);
  // antd Input 的 resolveOnChange 会 cloneNode(target)——事件 target/currentTarget 必须是真 DOM 节点
  await act(async () => {
    el.value = value; // 真实 input 事件时 DOM 已带新值；受控回写由 Form 再渲染完成
    p.onChange({ type: 'change', target: el, currentTarget: el, nativeEvent: {},
      preventDefault() {}, stopPropagation() {} });
  });
  await settle(20);
};

const SKILLS = [
  { skill_id: 's-1', skill_key: 'rag.retrieve', display_name: '审查检索技能', description: '检索',
    current_version: '1.1.0', state: 'active', updated_at: '2026-10-03T10:00:00Z', version_count: 2 },
  { skill_id: 's-2', skill_key: 'code.scan', display_name: '代码扫描', description: '扫描',
    current_version: null, state: 'disabled', updated_at: '2026-10-03T09:00:00Z', version_count: 0 },
];
const VERSIONS = [
  { version: '1.2.0', changelog: '新增过滤', manifest_sha256: 'a'.repeat(64), created_at: '2026-10-03T12:00:00Z', published_by: 'admin1' },
  { version: '1.1.0', changelog: '排序改进', manifest_sha256: 'b'.repeat(64), created_at: '2026-10-02T12:00:00Z', published_by: 'admin1' },
  { version: '1.0.0', changelog: '初始版', manifest_sha256: 'c'.repeat(64), created_at: '2026-10-01T12:00:00Z', published_by: 'admin1' },
];
const SKILL_ROUTES = () => ({
  ...COMMON,
  ...ADMIN_SESSION,
  '/api/mu/skills': () => [200, { skills: SKILLS }],
  '/api/mu/skills/rag.retrieve/versions': () => [200, { versions: VERSIONS }],
  // code.scan：发布过版本但从未激活（current_version=null）——「激活」动词路径
  '/api/mu/skills/code.scan/versions': () => [200, { versions: [
    { version: '0.9.0', changelog: '首版扫描', manifest_sha256: '9'.repeat(64), created_at: '2026-10-01T08:00:00Z', published_by: 'admin1' },
  ] }],
});

test('技能页：管理员看到列表+发布/历史入口+区分声明+三句话边界', async () => {
  ROUTES = SKILL_ROUTES();
  const { json } = await render('/skills');
  const text = json();
  try {
    assert.ok(text.includes('技能版本治理'), '治理面声明标题');
    assert.ok(text.includes('"/skills"'), '导航含 /skills 入口（B 波补遗回归锁——#290 曾漏导航项）');
    assert.ok(text.includes('发布=登记不可变版本'), '三句话边界之一：发布');
    assert.ok(text.includes('激活/回滚=切换当前生效版本'), '三句话边界之二：激活/回滚');
    assert.ok(text.includes('停用=暂停执行，不删除历史'), '三句话边界之三：停用');
    assert.ok(text.includes('一经发布不可变'), '不可变声明（页面文案）');
    assert.ok(text.includes('rag.retrieve') && text.includes('审查检索技能'), '技能行（key+显示名）');
    assert.ok(text.includes('1.1.0') && text.includes('未发布'), '当前版本列（生效+未发布两态）');
    assert.ok(text.includes('启用') && text.includes('停用'), '状态列两态');
    assert.ok(text.includes('注册技能') && text.includes('发布新版本') && text.includes('版本历史'), '管理入口');
    assert.ok(text.includes('与「知识库 / 知识检索（试用）」相互独立'), '与 RAG 试用区分声明');
    assert.ok(text.includes('调用统计与留痕不在本页'), 'C 波边界声明');
    assert.ok(!text.includes('演练'), '零 fixture 演练字样');
    assert.ok(!/[0-9a-f]{64}/.test(text), '列表页零完整 64 位指纹（不在治理面泄露工件指纹全文）');
    assert.ok(!/prompt|模型响应/i.test(text), '零 prompt/模型响应字样');
  } catch (e) { console.log(text.slice(0, 2500)); throw e; }
});

test('技能页：maintainer 只读（无注册/发布/停用入口）', async () => {
  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [200, {
      user: { user_id: 'u-2', login: 'm1' },
      tenant: { tenant_id: 't-1', slug: 'default', display_name: 'Default' },
      role: 'maintainer', actions: ['read_repository', 'decide_review'], memberships: [],
    }],
    '/api/mu/skills': () => [200, { skills: SKILLS }],
  };
  const { json } = await render('/skills');
  const text = json();
  try {
    assert.ok(text.includes('管理需平台管理员身份'), '只读提示');
    assert.ok(text.includes('版本历史'), '只读仍可看历史');
    assert.ok(!text.includes('注册技能') && !text.includes('发布新版本'), '写入口（注册/发布）零出现');
    assert.ok(!text.includes('停用技能') && !text.includes('启用技能'), '停用/启用确认框零出现（状态列标签除外）');
  } catch (e) { console.log(text.slice(0, 2500)); throw e; }
});

test('技能页：诚实空态与错误态（不回退演示数据）', async () => {
  ROUTES = { ...COMMON, ...ADMIN_SESSION, '/api/mu/skills': () => [200, { skills: [] }] };
  const empty = (await render('/skills')).json();
  assert.ok(empty.includes('暂无注册技能') && empty.includes('注册技能'), '空态+管理员引导');

  ROUTES = {
    ...COMMON,
    '/api/mu/session': () => [401, { error: { reason: 'unauthorized' } }],
    '/api/mu/skills': () => [401, { error: { reason: 'unauthorized' } }],
  };
  const denied = (await render('/skills')).json();
  assert.ok(denied.includes('技能列表不可用') && denied.includes('不回退演示数据'), '未登录诚实错误态');
});

test('技能页：结果反馈区带 role=status/aria-live（读写反馈统一播报）', async () => {
  ROUTES = SKILL_ROUTES();
  const { json } = await render('/skills');
  const text = json();
  assert.ok(text.includes('"role":"status"') && text.includes('"aria-live":"polite"'),
    '结果反馈容器带 role=status + aria-live=polite（常驻挂载）');
});

test('技能页：注册动作 busy 防重复——双击只发一次 POST', async () => {
  ROUTES = SKILL_ROUTES();
  let releaseReg;
  ROUTES['POST /api/mu/skills'] = () => new Promise((res) => { releaseReg = () => res([200, { ok: true, skill: {} }]); });
  const { close } = await renderDom('/skills');
  try {
    await domClick(domBtn('注册技能')); // 打开弹窗
    await domSetInput('input[placeholder="rag.retrieve"]', 'rag.new');
    await domSetInput('input[placeholder="审查检索技能"]', '新检索技能');
    const okBtn = domBtn('注册');
    assert.ok(okBtn, '弹窗 OK（注册）按钮存在');
    await domClick(okBtn); // 第 1 击：POST 在飞
    await domClick(okBtn); // 第 2 击：busy 拦截（confirmLoading 中）
    assert.equal(countCalls('POST', '/api/mu/skills'), 1, '双击只发一次 POST');
    await act(async () => { releaseReg(); });
    await settle();
    assert.ok(norm(document.body.textContent).includes('已注册'), '成功反馈可见');
  } finally { await close(); }
});

test('技能页：发布动作 busy 防重复 + current_version 真值展示 + 指纹文案', async () => {
  ROUTES = SKILL_ROUTES();
  const { close } = await renderDom('/skills');
  try {
    // ① 打开发布弹窗：指纹输入框说明=构建工具生成/非密钥（不谎报为密钥）
    await domClick(domBtn('发布新版本'));
    let text = norm(document.body.textContent);
    assert.ok(text.includes('由构建工具生成') && text.includes('非密钥'), '指纹说明：构建工具生成、非密钥');

    // ② 双击发布 → 仅一次 POST
    await domSetInput('input[placeholder="1.0.1"]', '1.2.0');
    await domSetInput('textarea[placeholder^="例：改进了检索排序"]', '新增过滤下推');
    await domSetInput('input[placeholder^="9f86d081884c7d659"]', 'd'.repeat(64));
    let releasePub;
    ROUTES['POST /api/mu/skills/rag.retrieve/versions'] = () => new Promise((res) => {
      releasePub = () => res([200, { ok: true, idempotent: false, version: { version: '1.2.0' },
        current_version: '1.1.0', activated: false }]); // 服务端真值：未自动切换
    });
    const okBtn = domBtn('发布');
    assert.ok(okBtn, '弹窗 OK（发布）按钮存在');
    await domClick(okBtn);
    await domClick(okBtn);
    assert.equal(countCalls('POST', '/api/mu/skills/rag.retrieve/versions'), 1, '双击发布只发一次 POST');

    // ③ 释放：响应 activated=false + current_version=1.1.0 → 展示真值，不猜
    await act(async () => { releasePub(); });
    await settle();
    text = norm(document.body.textContent);
    assert.ok(text.includes('未自动切换'), '未激活如实声明');
    assert.ok(text.includes('当前生效v1.1.0'), '展示服务端真值 current_version=1.1.0');
    assert.ok(text.includes('前往版本历史激活'), '提供「前往版本历史激活」直通按钮');

    // ④ 直通按钮 → 打开版本历史
    const before = countCalls('GET', '/api/mu/skills/rag.retrieve/versions');
    await domClick(domBtn('前往版本历史激活'));
    assert.ok(countCalls('GET', '/api/mu/skills/rag.retrieve/versions') > before, '直通按钮拉取版本历史');

    // ⑤ 再发布一版：响应 activated=true → 展示「已发布并激活为当前版本」
    await domClick(domBtn('发布新版本'));
    await domSetInput('input[placeholder="1.0.1"]', '1.3.0');
    await domSetInput('textarea[placeholder^="例：改进了检索排序"]', '修复过滤边界');
    await domSetInput('input[placeholder^="9f86d081884c7d659"]', 'e'.repeat(64));
    let releasePub2;
    ROUTES['POST /api/mu/skills/rag.retrieve/versions'] = () => new Promise((res) => {
      releasePub2 = () => res([200, { ok: true, idempotent: false, version: { version: '1.3.0' },
        current_version: '1.3.0', activated: true }]);
    });
    await domClick(domBtn('发布'));
    await act(async () => { releasePub2(); });
    await settle();
    assert.ok(norm(document.body.textContent).includes('已发布并激活为当前版本（当前生效v1.3.0）'), 'activated=true 展示真值');
  } finally { await close(); }
});

test('技能页：历史弹窗——当前版本行高亮且无双标注；激活/回滚 busy 防重复', async () => {
  ROUTES = SKILL_ROUTES();
  const { close } = await renderDom('/skills');
  try {
    await domClick(domBtn('版本历史'));
    let text = norm(document.body.textContent);
    assert.ok(text.includes('v1.2.0') && text.includes('当前生效'), '历史行渲染（含当前生效标注一次）');
    assert.ok(!text.includes('生效中'), '去掉「当前生效/生效中」双重标注');
    assert.equal(qAll('tr.ant-table-row-selected').length, 1, '当前版本行恰好一行高亮');

    // 前向切换（1.1.0→1.2.0）：已有生效版本时切换入口统一为「回滚到此」；双击确认 → 一次 POST
    let releaseAct;
    ROUTES['POST /api/mu/skills/rag.retrieve/activate'] = () => new Promise((res) => {
      releaseAct = () => res([200, { ok: true, idempotent: false, current_version: '1.2.0', rollback: false }]);
    });
    await domClick(domBtn('回滚到此')); // 打开 Popconfirm
    let confirm = domBtn('确定');
    assert.ok(confirm, '切换确认按钮存在');
    await domClick(confirm);
    await domClick(confirm); // busy 拦截
    assert.equal(countCalls('POST', '/api/mu/skills/rag.retrieve/activate'), 1, '双击切换只发一次 POST');
    await act(async () => { releaseAct(); });
    await settle();
    assert.ok(norm(document.body.textContent).includes('已激活1.2.0'), '前向切换反馈（激活）');

    // 回滚（切到更低 1.0.0）：双击确认 → 一次 POST → rollback 反馈
    let releaseRoll;
    ROUTES['POST /api/mu/skills/rag.retrieve/activate'] = () => new Promise((res) => {
      releaseRoll = () => res([200, { ok: true, idempotent: false, current_version: '1.0.0', rollback: true }]);
    });
    const rollBtns = qAll('button').filter((b) => norm(b.textContent) === '回滚到此');
    assert.ok(rollBtns.length >= 2, '存在多个可切换版本行');
    await domClick(rollBtns[rollBtns.length - 1]); // 取最后一行（v1.0.0）作为回滚目标
    confirm = domBtn('确定');
    assert.ok(confirm, '回滚确认按钮存在');
    await domClick(confirm);
    await domClick(confirm);
    assert.equal(countCalls('POST', '/api/mu/skills/rag.retrieve/activate'), 2, '激活+回滚各一次 POST');
    await act(async () => { releaseRoll(); });
    await settle();
    assert.ok(norm(document.body.textContent).includes('已回滚到1.0.0'),
      `回滚反馈：${norm(document.body.textContent).slice(-260)}`);

    // 「激活」动词路径：从未激活的技能（code.scan，current_version=null）历史弹窗内按钮为「激活」
    const histBtns = qAll('button').filter((b) => norm(b.textContent) === '版本历史');
    assert.ok(histBtns.length >= 2, '两个技能均有版本历史入口');
    await domClick(histBtns[1]); // code.scan 行
    await settle(60);
    assert.ok(norm(document.body.textContent).includes('v0.9.0'), 'code.scan 历史行渲染');
    const actBtn = domBtn('激活');
    assert.ok(actBtn, '未激活技能历史行显示「激活」');
    let releaseNull;
    ROUTES['POST /api/mu/skills/code.scan/activate'] = () => new Promise((res) => {
      releaseNull = () => res([200, { ok: true, idempotent: false, current_version: '0.9.0', rollback: false }]);
    });
    await domClick(actBtn);
    const confirmNull = domBtn('确定');
    assert.ok(confirmNull, '激活确认按钮存在');
    await domClick(confirmNull);
    await domClick(confirmNull); // busy 拦截
    assert.equal(countCalls('POST', '/api/mu/skills/code.scan/activate'), 1, '双击激活只发一次 POST');
    await act(async () => { releaseNull(); });
    await settle();
    assert.ok(norm(document.body.textContent).includes('已激活0.9.0'), '激活反馈');
  } finally { await close(); }
});

test('技能页：停用/启用 busy 防重复 + 停用语义 tooltip', async () => {
  ROUTES = SKILL_ROUTES();
  const { close } = await renderDom('/skills');
  try {
    // 状态 Tag 外层有 Tooltip（懒渲染）——直接驱动 onMouseEnter 验证文案进入 body
    const statusTag = qAll('.ant-table-tbody .ant-tag.ant-tag-red')[0];
    assert.ok(statusTag, '状态 Tag（停用/红色）存在');
    const tagProps = reactProps(statusTag);
    assert.ok(tagProps && typeof tagProps.onMouseEnter === 'function', '状态 Tag 挂 Tooltip 事件');
    await act(async () => { tagProps.onMouseEnter({ preventDefault() {}, stopPropagation() {} }); });
    await settle(120);
    assert.ok(norm(document.body.textContent).includes('重新启用即恢复'),
      '停用语义 tooltip：审查执行栈不再调用、版本保留、重新启用即恢复');

    let releaseToggle;
    ROUTES['POST /api/mu/skills/rag.retrieve/disable'] = () => new Promise((res) => {
      releaseToggle = () => res([200, { ok: true, idempotent: false, state: 'disabled' }]);
    });
    await domClick(domBtn('停用')); // 打开 Popconfirm
    const confirm = domBtn('确定');
    assert.ok(confirm, '停用确认按钮存在');
    await domClick(confirm);
    await domClick(confirm); // busy 拦截
    assert.equal(countCalls('POST', '/api/mu/skills/rag.retrieve/disable'), 1, '双击停用只发一次 POST');
    await act(async () => { releaseToggle(); });
    await settle();
    assert.ok(norm(document.body.textContent).includes('已停用'), '停用反馈');
  } finally { await close(); }
});

test('技能页：错误 reason 机器码映射中文，不直出机器码', async () => {
  ROUTES = SKILL_ROUTES();
  ROUTES['POST /api/mu/skills/rag.retrieve/versions'] = () => [409,
    { error: { reason: 'version_immutable_conflict', detail: '该版本号已发布且指纹不同' } }];
  const { close } = await renderDom('/skills');
  try {
    await domClick(domBtn('发布新版本'));
    await domSetInput('input[placeholder="1.0.1"]', '1.1.0'); // 同版本号
    await domSetInput('textarea[placeholder^="例：改进了检索排序"]', '换指纹尝试');
    await domSetInput('input[placeholder^="9f86d081884c7d659"]', 'f'.repeat(64));
    await domClick(domBtn('发布'));
    await settle();
    let text = norm(document.body.textContent);
    assert.ok(text.includes('请递增版本号'), 'version_immutable_conflict → 中文映射');
    assert.ok(!text.includes('version_immutable_conflict'), '机器码不直出');

    ROUTES['POST /api/mu/skills'] = () => [409, { error: { reason: 'skill_key_exists' } }];
    await domClick(domBtn('注册技能'));
    await domSetInput('input[placeholder="rag.retrieve"]', 'rag.retrieve'); // 撞已有 key
    await domSetInput('input[placeholder="审查检索技能"]', '重复技能');
    await domClick(domBtn('注册'));
    await settle();
    text = norm(document.body.textContent);
    assert.ok(text.includes('该技能标识已存在'), 'skill_key_exists → 中文映射');

    // 网络异常 → 「网络错误」话术（fetch reject 被 try/catch 收口，不白屏）
    ROUTES['POST /api/mu/skills/rag.retrieve/activate'] = () => { throw new Error('boom'); };
    await domClick(domBtn('版本历史'));
    await domClick(domBtn('回滚到此'));
    await domClick(domBtn('确定'));
    await settle();
    assert.ok(norm(document.body.textContent).includes('网络错误'), '网络异常 → 诚实话术');
  } finally { await close(); }
});

test('技能页：版本历史加载失败可恢复（错误块+重试），不永久「加载中」', async () => {
  ROUTES = SKILL_ROUTES();
  ROUTES['/api/mu/skills/rag.retrieve/versions'] = () => [500, { error: { reason: 'internal' } }];
  const { close } = await renderDom('/skills');
  try {
    await domClick(domBtn('版本历史'));
    const text = norm(document.body.textContent);
    assert.ok(text.includes('版本历史加载失败'), '失败态错误块');
    assert.ok(text.includes('重试'), '提供重试入口');
    assert.ok(!text.includes('加载中'), '不再停留「加载中…」');

    ROUTES['/api/mu/skills/rag.retrieve/versions'] = () => [200, { versions: VERSIONS }];
    await domClick(domBtn('重试'));
    const text2 = norm(document.body.textContent);
    assert.ok(text2.includes('v1.0.0') && text2.includes('当前生效'), '重试后恢复渲染历史');
  } finally { await close(); }
});

// react-dom scheduler（MessageChannel）与 happy-dom 定时器会让事件循环挂住——全部用例结束后强制收敛
after(() => {
  try { domWindow.happyDOM?.abort?.(); } catch { /* 忽略 */ }
  setTimeout(() => process.exit(process.exitCode ?? 0), 50);
});
