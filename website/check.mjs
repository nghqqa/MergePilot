#!/usr/bin/env node
// 官网静态检查：内部链接解析 + 页面孤岛 + 开发机依赖扫描。零依赖。
import fs from 'node:fs';
import path from 'node:path';

const ROOT = process.argv[2] || new URL('.', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? pass++ : fail++; console.log((c ? '  PASS  ' : '  FAIL  ') + n + (d ? '  ' + d : '')); };

const pages = fs.readdirSync(ROOT).filter((f) => f.endsWith('.html'));
const cssExists = fs.existsSync(path.join(ROOT, 'assets', 'style.css'));
ok('assets/style.css 存在', cssExists);

const internalRefs = new Set();
for (const p of pages) {
  const html = fs.readFileSync(path.join(ROOT, p), 'utf8');
  for (const m of html.matchAll(/(?:href|src)="([^"]+)"/g)) {
    const ref = m[1];
    if (/^https?:/.test(ref) || ref.startsWith('#') || ref.startsWith('mailto:')) continue;
    const clean = ref.split('#')[0].split('?')[0];
    internalRefs.add(clean === '' ? '.' : clean);
  }
  let styled = /assets\/style\.css/.test(html);
  if (!styled) { fail++; console.log('  FAIL  ' + p + ' 未引用样式表'); }
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
