#!/usr/bin/env node
// 导航行为测试（零依赖）：加载真实 assets/nav.js，在最小 DOM shim 上验证
// 菜单展开/关闭、aria-expanded 同步、Escape 关闭并还焦点、链接点击关闭菜单、
// no-js 类移除。CSS 可见性由 check.mjs 校验，此处验证 JS 接线。
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';

const require = createRequire(import.meta.url);
const navJs = require(path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), 'assets', 'nav.js'));
const { initNav } = navJs;

let pass = 0, fail = 0;
const ok = (name, cond, detail) => { cond ? pass++ : fail++; console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (detail ? '  ' + detail : '')); };

function classList() {
  const set = new Set();
  return {
    add: (...c) => c.forEach((x) => set.add(x)),
    remove: (...c) => c.forEach((x) => set.delete(x)),
    toggle: (c, force) => { const on = force !== undefined ? force : !set.has(c); on ? set.add(c) : set.delete(c); return on; },
    contains: (c) => set.has(c),
  };
}
function el(attrs = {}) {
  const handlers = {};
  return {
    classList: classList(),
    attrs: { ...attrs },
    dataset: {},
    focused: false,
    addEventListener: (t, h) => { (handlers[t] = handlers[t] || []).push(h); },
    setAttribute: (k, v) => { attrs[k] = v; },
    getAttribute: (k) => attrs[k],
    focus() { this.focused = true; },
    dispatch(type, ev = {}) {
      (handlers[type] || []).forEach((h) => h({ target: ev.target || null, key: ev.key, ...ev }));
    },
  };
}
function fakeDoc() {
  const toggle = el({ 'aria-expanded': 'false', 'aria-controls': 'nav-menu' });
  const nav = el();
  const menu = el();
  const root = el({ class: 'no-js' });
  const doc = {
    documentElement: root,
    getElementById: (id) => (id === 'nav-menu' ? menu : null),
    querySelector: (sel) => (sel === '.nav-toggle' ? toggle : sel === '.nav' ? nav : null),
  };
  // doc 自身也要有 addEventListener（keydown 绑定在 doc 上）
  Object.assign(doc, el());
  return { doc, toggle, nav, menu, root };
}

// 页面标记契约：每个 HTML 页面都带 no-js 初始类与菜单按钮（check.mjs 另查）
{
  const { doc, toggle, nav, menu, root } = fakeDoc();
  initNav(doc);
  ok('init 移除 no-js 并加 js', !root.classList.contains('no-js') && root.classList.contains('js'));

  const fresh = fakeDoc();
  ok('初始 aria-expanded=false（页面标记契约）', fresh.toggle.getAttribute('aria-expanded') === 'false');

  const t2 = fakeDoc();
  initNav(t2.doc);
  t2.toggle.dispatch('click');
  ok('点击展开：aria-expanded=true + nav-open', t2.toggle.getAttribute('aria-expanded') === 'true' && t2.nav.classList.contains('nav-open'));
  t2.toggle.dispatch('click');
  ok('再次点击关闭：aria-expanded=false', t2.toggle.getAttribute('aria-expanded') === 'false' && !t2.nav.classList.contains('nav-open'));

  const t3 = fakeDoc();
  initNav(t3.doc);
  t3.toggle.dispatch('click');
  t3.doc.dispatch('keydown', { key: 'Escape' });
  ok('Escape 关闭菜单', t3.toggle.getAttribute('aria-expanded') === 'false' && !t3.nav.classList.contains('nav-open'));
  ok('Escape 后焦点回到菜单按钮', t3.toggle.focused === true);

  const t4 = fakeDoc();
  initNav(t4.doc);
  t4.doc.dispatch('keydown', { key: 'Escape' });
  ok('未展开时 Escape 不抢焦点', t4.toggle.focused === false && t4.toggle.getAttribute('aria-expanded') === 'false');

  const t5 = fakeDoc();
  initNav(t5.doc);
  t5.toggle.dispatch('click');
  const link = { closest: (s) => (s === 'a' ? { href: 'docs.html' } : null) };
  t5.menu.dispatch('click', { target: link });
  ok('点击菜单内链接后关闭（跳转由浏览器原生完成）', t5.toggle.getAttribute('aria-expanded') === 'false');

  const t6 = fakeDoc();
  delete t6.doc.querySelector; // 缺按钮/菜单时安全退出，不抛错
  t6.doc.querySelector = () => null;
  let threw = false;
  try { initNav(t6.doc); } catch { threw = true; }
  ok('DOM 缺导航元素时静默退出不抛错', !threw);
}

// nav.js 以 UMD 导出且浏览器路径自动初始化（typeof document 守卫存在）
const src = fs.readFileSync(path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), 'assets', 'nav.js'), 'utf8');
ok('nav.js 含 no-js 移除逻辑（脚本失败时 CSS 常显回退生效）', /classList\.remove\('no-js'\)/.test(src));

console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
