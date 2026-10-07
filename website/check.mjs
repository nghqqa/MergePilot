#!/usr/bin/env node
// 官网静态检查：导航一致性 + 内部链接解析 + 页面孤岛 + 文档容器 + a11y 标记
// + no-js 回退 + 开发机依赖扫描。零依赖。
// 导航单一来源见 nav.mjs（node nav.mjs 回写、node nav.mjs --check 查漂移）。
// 行为接线测试见 behavior-test.mjs（真实 nav.js + DOM shim）。
import fs from 'node:fs';
import path from 'node:path';
import { renderTopNav, renderDocsNav, TOP_ITEMS, DOC_GROUPS, DOCS_INDEX_HREF, isDocPage } from './nav.mjs';

const ROOT = process.argv[2] || new URL('.', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? pass++ : fail++; console.log((c ? '  PASS  ' : '  FAIL  ') + n + (d ? '  ' + d : '')); };

const pages = fs.readdirSync(ROOT).filter((f) => f.endsWith('.html'));
const cssExists = fs.existsSync(path.join(ROOT, 'assets', 'style.css'));
ok('assets/style.css 存在', cssExists);
ok('assets/nav.js 存在', fs.existsSync(path.join(ROOT, 'assets', 'nav.js')));

const region = (html, name) => {
  const m = html.match(new RegExp(`<!-- nav:${name} -->[\\s\\S]*?<!-- /nav:${name} -->`));
  return m ? m[0].replace(/\r\n/g, '\n') : null;
};

const internalRefs = new Set();
for (const p of pages) {
  let html = fs.readFileSync(path.join(ROOT, p), 'utf8');
  for (const m of html.matchAll(/(?:href|src)="([^"]+)"/g)) {
    const ref = m[1];
    if (/^https?:/.test(ref) || ref.startsWith('#') || ref.startsWith('mailto:')) continue;
    const clean = ref.split('#')[0].split('?')[0];
    internalRefs.add(clean === '' ? '.' : clean);
  }
  ok(p + ' 引用样式表', /assets\/style\.css/.test(html));

  // ── 导航一致性（与 nav.mjs 单一来源逐字节比对）──
  ok('顶栏与单一来源一致: ' + p, region(html, 'top') === renderTopNav(p));
  const expectDocs = isDocPage(p);
  ok((expectDocs ? '侧栏与单一来源一致' : '无侧栏（正确）') + ': ' + p,
    expectDocs ? region(html, 'docs') === renderDocsNav(p) : !region(html, 'docs'));

  // ── 顶栏 aria-current 精确性 ──
  const top = region(html, 'top') || '';
  const tops = [...top.matchAll(/<a href="([^"]+)"( aria-current="([a-z]+)")?>/g)];
  const topLinks = tops.map((m) => m[1]);
  ok('顶栏项与清单一致(顺序): ' + p, JSON.stringify(topLinks) === JSON.stringify(TOP_ITEMS.map((t) => t.href)));
  const isTop = TOP_ITEMS.some((t) => t.href === p);
  const expCur = isTop ? p : (isDocPage(p) ? DOCS_INDEX_HREF : null);
  const curMarks = tops.filter((m) => m[3]).map((m) => [m[1], m[3]]);
  ok('顶栏 aria-current 正确: ' + p,
    curMarks.length === (expCur ? 1 : 0) && (!expCur || (curMarks[0][0] === expCur && curMarks[0][1] === (isTop ? 'page' : 'true'))),
    curMarks.map((c) => c.join('=')).join(',') || '无');

  // ── a11y 接线：no-js 回退 + 菜单按钮 ──
  ok('html.no-js 初始类: ' + p, /<html lang="zh-CN" class="no-js">/.test(html));
  ok('nav.js 引用(defer)且仅一次: ' + p, (html.match(/<script src="assets\/nav\.js" defer><\/script>/g) || []).length === 1);
  ok('菜单按钮 aria-expanded/aria-controls: ' + p,
    /<button class="nav-toggle" type="button" aria-expanded="false" aria-controls="nav-menu">菜单<\/button>/.test(html));
  ok('菜单容器 id=nav-menu: ' + p, /id="nav-menu"/.test(html));

  // ── 容器与 footer ──
  if (isDocPage(p)) {
    ok('文档 shell 容器(shell+aside+article): ' + p,
      /<main class="shell">\s*<!-- nav:docs -->/.test(html) &&
      /<!-- \/nav:docs -->\s*<article class="doc">/.test(html) && html.includes('</article>\n</main>'));
  } else {
    ok('首页 main 配对且无文档容器: ' + p,
      (html.match(/<main>/g) || []).length === 1 && (html.match(/<\/main>/g) || []).length === 1 && !/class="doc"/.test(html));
  }
  ok('footer 统一 class="footer": ' + p, /<footer class="footer">/.test(html) && !/<footer class="site">/.test(html));
}
for (const ref of internalRefs) {
  const target = path.join(ROOT, ref);
  ok('链接解析: ' + ref, fs.existsSync(target));
}
// 孤岛检查：每页都应被至少一个页面（或自身导航）引用
for (const p of pages) {
  const referenced = pages.some((q) => q !== p && fs.readFileSync(path.join(ROOT, q), 'utf8').includes('"' + p + '"'));
  ok('页面可达（被导航引用）: ' + p, referenced || p === 'index.html');
}
// 文档索引正文（导航区域之外）覆盖全部分组条目
{
  let idx = fs.readFileSync(path.join(ROOT, DOCS_INDEX_HREF), 'utf8');
  idx = idx.replace(/<!-- nav:top -->[\s\S]*?<!-- \/nav:top -->/, '').replace(/<!-- nav:docs -->[\s\S]*?<!-- \/nav:docs -->/, '');
  const missing = DOC_GROUPS.flatMap((g) => g.items.map((i) => i.href)).filter((h) => !idx.includes('"' + h + '"'));
  ok('文档索引正文覆盖全部分组条目', missing.length === 0, missing.join(','));
  const descs = DOC_GROUPS.flatMap((g) => g.items.map((i) => i.href));
  ok('文档分组非空', descs.length > 0);
}
// 侧栏 aria-current 精确性：文档正文页恰一处 page 标记且落在侧栏内；索引页侧栏无标记（顶栏承担）
for (const p of pages.filter((x) => isDocPage(x))) {
  const html = fs.readFileSync(path.join(ROOT, p), 'utf8');
  const docs = region(html, 'docs') || '';
  if (p === DOCS_INDEX_HREF) {
    ok('索引页侧栏无 aria-current（顶栏文档项承担）: ' + p, !/aria-current/.test(docs));
  } else {
    const inSide = docs.match(new RegExp(`<a href="${p.replace(/\./g, '\\.')}" aria-current="page">`));
    const outside = html.replace(/<!-- nav:top -->[\s\S]*?<!-- \/nav:top -->/, '').replace(/<!-- nav:docs -->[\s\S]*?<!-- \/nav:docs -->/, '');
    ok('侧栏 aria-current 恰在当前页: ' + p, !!inSide && !/aria-current="page"/.test(outside));
  }
}
// CSS 事实：no-js 回退、移动菜单、代码块/表格横向滚动、阅读容器
{
  const css = cssExists ? fs.readFileSync(path.join(ROOT, 'assets', 'style.css'), 'utf8') : '';
  ok('CSS no-js 回退（菜单常显/按钮隐藏）', /html\.no-js \.nav-links \{ display: flex; \}/.test(css) && /html\.no-js \.nav-toggle \{ display: none; \}/.test(css));
  ok('CSS 移动端菜单由 nav-open 驱动', /\.nav\.nav-open \.nav-links \{ display: flex; \}/.test(css));
  ok('CSS 文档 shell 双栏 + 阅读宽度', /\.shell \{[^}]*grid-template-columns: 200px minmax\(0, 1fr\)/.test(css) && /\.shell > \.doc \{ max-width: 760px/.test(css));
  ok('CSS pre 横向滚动（页面不溢出）', /pre \{[^}]*overflow-x: auto/.test(css));
  ok('CSS 窄屏表格块级横向滚动', css.includes('table { display: block; overflow-x: auto'));
  ok('CSS table-wrap 可用', /\.table-wrap \{ overflow-x: auto/.test(css));
}
// 开发机依赖扫描（精确模式：抓内部组件名、开发机路径、"内部入口被宣称为官方"）
const banned = [/D:\\/i, /\/d\/goai/i, /frpc/i, /beta-webhook-proxy/i, /rc15-pr325/i, /aa53b09f/i,
  /48590[^。<\n]{0,24}(官方|正式|托管入口)/, /(官方|正式|托管入口)[^。<\n]{0,24}48590/];
let hits = [];
for (const p of pages) {
  const html = fs.readFileSync(path.join(ROOT, p), 'utf8');
  for (const re of banned) if (re.test(html)) hits.push(p + ': ' + re);
}
ok('无开发机路径/内部组件/内部地址依赖', hits.length === 0, hits.join('; '));
// 版本一致性（精确模式：抓会被用户执行的指令中的旧版本，历史陈述不误伤）
for (const p of pages) {
  const html = fs.readFileSync(path.join(ROOT, p), 'utf8');
  if (/v0\.2\.0-beta\.6-rc\.14/.test(html)) { fail++; console.log('  FAIL  ' + p + ' 指令/引用残留 rc.14 版本 tag'); }
  if (/0\.2\.0-beta\.6-rc\.14[）`」（]/.test(html)) { fail++; console.log('  FAIL  ' + p + ' 残留 rc.14 完整版本号表述'); }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
