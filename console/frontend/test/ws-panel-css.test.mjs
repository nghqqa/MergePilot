// ws-panel-css.test.mjs — WorkspaceStatusPanel ws-* 样式完整性守卫（PR-1 全局 P0）。
// 背景：console.css 此前只有 .ws-wrap 一条规则，组件使用的 ~20 个 ws-* 类全部无样式，
// 顶栏弹层 static 裸渲染、被视口裁剪、与正文叠字，/datasources 退化为文字墙。
// 本测试锁定两件事：
//   1) 源码里出现的每个 ws-* 类（含 ws-tone-* / ws-dot-* 动态取值）在 console.css 都有选择器；
//   2) CSS 里不存在孤儿 ws-* 选择器（源码已删类而样式残留）。
// 另断言弹层定位关键属性（绝对定位/右对齐/白底/发丝线/高度上限/内部滚动/窄屏底部抽屉）。
// 运行：cd console/frontend && node --test test/ws-panel-css.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND = path.resolve(__dirname, '..');
const SRC = path.join(FRONTEND, 'src');
const css = fs.readFileSync(path.join(SRC, 'console.css'), 'utf8');

// —— 提取 src 下全部源码文本（排除 console.css 自身）——
function collectSourceText(dir) {
  let out = '';
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) out += collectSourceText(p);
    else if (/\.(jsx?|mjs|tsx?)$/.test(entry.name)) out += fs.readFileSync(p, 'utf8');
  }
  return out;
}
const sourceText = collectSourceText(SRC);

// —— 源码用到的 ws-* 类（console.css 排除在外）——
const TONE_VARIANTS = ['ws-tone-ok', 'ws-tone-warn', 'ws-tone-bad', 'ws-tone-neutral'];
const DOT_VARIANTS = ['ws-dot-ok', 'ws-dot-no'];

function usedWsClasses(text) {
  const found = new Set();
  const dynamic = new Set();
  for (const m of text.matchAll(/ws-[a-z0-9-]+/g)) {
    const raw = m[0];
    if (/-[0-9]*$/.test(raw) && raw.endsWith('-')) {
      dynamic.add(raw.slice(0, -1)); // 模板串动态前缀（如 `ws-tone-${tone}`）→ 'ws-tone'
    } else {
      found.add(raw);
    }
  }
  return { found: [...found], dynamic: [...dynamic] };
}
const { found: usedClasses, dynamic: usedDynamic } = usedWsClasses(sourceText);

// —— CSS 中定义的 ws-* 选择器（朴素解析：.ws-xxx 出现在选择器位置）——
function definedWsSelectors(text) {
  const sels = new Set();
  // 去掉注释，避免示例文本误报
  const noComment = text.replace(/\/\*[\s\S]*?\*\//g, '');
  for (const m of noComment.matchAll(/\.((?:ws-)[a-z0-9-]+)/g)) sels.add(m[1]);
  return sels;
}
const defined = definedWsSelectors(css);

test('源码使用的每个 ws-* 类都在 console.css 有选择器（无裸渲染类）', () => {
  const missing = usedClasses.filter((c) => !defined.has(c));
  assert.deepEqual(missing, [], `以下 ws-* 类被组件使用但 console.css 未定义: ${missing.join(', ')}`);
});

test('动态 ws-tone-* / ws-dot-* 全部取值都有样式', () => {
  assert.ok(usedDynamic.includes('ws-tone'), '源码应使用 ws-tone-* 动态前缀（若重构请同步更新本测试）');
  for (const t of TONE_VARIANTS) assert.ok(defined.has(t), `缺少 .${t} 选择器`);
  assert.ok(usedClasses.includes('ws-dot'), '源码应使用基础 .ws-dot 类');
  for (const d of DOT_VARIANTS) assert.ok(defined.has(d), `缺少 .${d} 选择器`);
});

test('console.css 无孤儿 ws-* 选择器（样式不引用已删除的类）', () => {
  const allowed = new Set([...usedClasses, ...TONE_VARIANTS, ...DOT_VARIANTS]);
  const orphans = [...defined].filter((c) => !allowed.has(c));
  assert.deepEqual(orphans, [], `console.css 定义了源码未使用的 ws-* 类: ${orphans.join(', ')}`);
});

// —— 按 CSS 选择器取声明块（花括号配平），断言弹层定位关键属性 ——
function blockOf(selector) {
  const idx = css.indexOf(selector);
  if (idx === -1) return null;
  const open = css.indexOf('{', idx);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === '{') depth++;
    else if (css[i] === '}') { depth--; if (depth === 0) return css.slice(open + 1, i); }
  }
  return null;
}

test('.ws-wrap 建立定位上下文（弹层锚点前提）', () => {
  const b = blockOf('.ws-wrap {');
  assert.ok(b, '缺少 .ws-wrap 规则');
  assert.match(b.replace(/\s+/g, ' '), /position:\s*relative/);
});

test('.ws-pop 绝对定位于 chip 下方、右对齐、白底、发丝线、有界可滚', () => {
  const b = (blockOf('.ws-pop {') || '').replace(/\s+/g, ' ');
  assert.ok(b, '缺少 .ws-pop 规则');
  assert.match(b, /position:\s*absolute/);
  assert.match(b, /top:\s*calc\(100%\s*\+\s*8px\)/, '弹层应锚在 chip 下方 8px');
  assert.match(b, /right:\s*0/, '弹层右缘应与顶栏 chip 对齐');
  assert.match(b, /z-index:\s*\d+/);
  assert.match(b, /background:\s*var\(--c-panel\)/, '弹层必须有非透明白底');
  assert.match(b, /border:\s*var\(--border-w\)\s*solid\s*var\(--c-border\)/, '1px 发丝线边框');
  assert.match(b, /border-radius:\s*var\(--r-lg\)/);
  assert.match(b, /box-shadow:\s*var\(--shadow-md\)/, '柔和阴影');
  assert.match(b, /max-height:\s*calc\(100vh/, '矮视口不得溢出（高度上限）');
  const inner = (blockOf('.ws-pop .ws-panel {') || '').replace(/\s+/g, ' ');
  assert.match(inner, /overflow-y:\s*auto/, '长内容应内部滚动');
  const foot = (blockOf('.ws-pop-foot {') || '').replace(/\s+/g, ' ');
  assert.ok(foot, '缺少 .ws-pop-foot 规则（收起钮所在区域）');
});

test('窄屏（≤960px）弹层改近全宽定位，不越出视口', () => {
  // 文件里有多个 @media (max-width: 960px) 块——定位包含 .ws-pop 的那一个
  const hits = [...css.matchAll(/@media \(max-width: 960px\)/g)];
  assert.ok(hits.length > 0, '缺少 ≤960px 媒体查询');
  const hit = hits.map((h) => {
    const start = h.index;
    const popIdx = css.indexOf('.ws-pop', start);
    return popIdx === -1 ? null : { start, popIdx };
  }).find(Boolean);
  assert.ok(hit, '≤960px 媒体查询内应对 .ws-pop 重定位');
  const b = css.slice(hit.start, css.indexOf('}', hit.popIdx) + 1).replace(/\s+/g, ' ');
  assert.match(b, /\.ws-pop\s*\{[^}]*position:\s*fixed/);
  assert.match(b, /\.ws-pop\s*\{[^}]*left:\s*10px/);
  assert.match(b, /\.ws-pop\s*\{[^}]*right:\s*10px/);
  assert.match(b, /\.ws-pop\s*\{[^}]*max-height:[^;]*vh/);
});

test('/datasources 层级：label 11.5px/600/三级色，value 13px/主色，状态点可见', () => {
  const label = (blockOf('.ws-row-label {') || '').replace(/\s+/g, ' ');
  assert.match(label, /font-size:\s*var\(--fs-xs\)/); // 11.5px
  assert.match(label, /font-weight:\s*var\(--fw-semibold\)/); // 600
  assert.match(label, /color:\s*var\(--c-text-3\)/); // 三级色
  const value = (blockOf('.ws-row-value {') || '').replace(/\s+/g, ' ');
  assert.match(value, /font-size:\s*var\(--fs-sm\)/); // 13px
  assert.match(value, /color:\s*var\(--c-text\)/); // 主色
  const dot = (blockOf('.ws-dot {') || '').replace(/\s+/g, ' ');
  assert.match(dot, /width:\s*8px/);
  assert.match(dot, /background:\s*var\(--c-neutral-dot\)/);
  assert.match((blockOf('.ws-dot-ok {') || ''), /var\(--c-ok-dot\)/);
});

test('App.jsx 保留 Esc 关闭与 dialog 语义（结构适配不回归）', () => {
  const app = fs.readFileSync(path.join(SRC, 'App.jsx'), 'utf8');
  assert.match(app, /aria-haspopup="dialog"/);
  assert.match(app, /role="dialog"/);
  assert.match(app, /aria-expanded=\{open\}/);
  assert.match(app, /key === 'Escape'/, '应有 Esc 键盘关闭处理');
  assert.match(app, /addEventListener\('keydown'/, 'Esc 应通过 keydown 监听实现');
});
