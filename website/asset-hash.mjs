#!/usr/bin/env node
// 官网静态资源内容哈希与引用同步（零依赖，node >= 20）。
//
// 用法：
//   node asset-hash.mjs            # 哈希 + 重命名 + 更新 HTML 引用（幂等）
//   node asset-hash.mjs --check    # 只校验不写入（exit 1 = 漂移）
//   node asset-hash.mjs --restore  # 恢复固定名引用（开发模式）
//
// 原理：对 style.css / nav.js 计算 SHA256 前 8 位作为内容指纹，
// 生成 style.{hash}.css / nav.{hash}.js 副本，并把全部 HTML 引用
// 从固定名改为哈希名。HTML 本身不加哈希（浏览器每次重新校验）。
// 固定名源文件保留在 assets/ 下作为编辑入口；哈希副本仅供部署。
// 幂等：同内容 → 同哈希 → 同文件名；内容变化 → 哈希变化 → 新文件。
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = new URL('.', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const ASSETS = path.join(ROOT, 'assets');
const SOURCES = [
  { src: 'style.css', type: 'css' },
  { src: 'nav.js', type: 'js' },
];

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

// 读取当前 HTML 文件列表
function htmlFiles() {
  return fs.readdirSync(ROOT).filter((f) => f.endsWith('.html')).sort();
}

// 对全部 HTML 执行 replace 并计数
function replaceInHtml(files, search, replace) {
  let n = 0;
  for (const f of files) {
    const p = path.join(ROOT, f);
    const s = fs.readFileSync(p, 'utf8');
    if (s.includes(search)) {
      fs.writeFileSync(p, s.split(search).join(replace), 'utf8');
      n++;
    }
  }
  return n;
}

// ── 哈希函数 ──
function contentHash(buf) {
  return sha256(buf).slice(0, 8);
}

// ── 主逻辑 ──
const mode = process.argv[2] || '';

if (mode === '--check') {
  // 校验模式：验证哈希资源存在 + HTML 引用一致 + 无旧固定名引用
  let fail = 0;
  const ok = (msg, cond, detail) => {
    if (!cond) fail++;
    console.log((cond ? '  PASS  ' : '  FAIL  ') + msg + (detail && !cond ? '  ' + detail : ''));
  };

  for (const { src, type } of SOURCES) {
    const ext = type;
    const files = fs.readdirSync(ASSETS).filter((f) => f.startsWith(src.replace(`.${ext}`, '')) && f.endsWith(`.${ext}`));
    const hashed = files.filter((f) => /\.[0-9a-f]{8}\./.test(f));
    if (hashed.length === 0) {
      ok(`${src}: 存在内容哈希副本`, false, '未找到哈希命名副本——请运行 node asset-hash.mjs');
      continue;
    }
    // 每个哈希副本的字节必须与源文件一致
    const srcBuf = fs.readFileSync(path.join(ASSETS, src));
    for (const h of hashed) {
      const copyBuf = fs.readFileSync(path.join(ASSETS, h));
      ok(`${h}: 内容与源 ${src} 一致`, copyBuf.equals(srcBuf));
    }
  }

  // HTML 引用检查：不应引用未哈希的固定名
  for (const f of htmlFiles()) {
    const html = fs.readFileSync(path.join(ROOT, f), 'utf8');
    const stale = html.match(/href="assets\/style\.css"|src="assets\/nav\.js"/g);
    ok(`${f}: 无旧固定名资产引用`, !stale, stale ? '发现 ' + stale.length + ' 处固定名引用' : '');
  }

  process.exit(fail ? 1 : 0);
}

// ── 生成模式 ──
// 1. 读取源文件内容
const entries = SOURCES.map(({ src, type }) => {
  const buf = fs.readFileSync(path.join(ASSETS, src));
  return { src, type, buf, hash: contentHash(buf) };
});

// 2. 生成哈希副本
for (const e of entries) {
  const hashedName = e.src.replace(/(\.\w+)$/, `.${e.hash}$1`);
  const hashedPath = path.join(ASSETS, hashedName);
  const exists = fs.existsSync(hashedPath);
  if (!exists || !fs.readFileSync(hashedPath).equals(e.buf)) {
    fs.writeFileSync(hashedPath, e.buf);
    console.log(`  生成 ${hashedName} (${e.buf.length} bytes, hash=${e.hash})`);
  } else {
    console.log(`  ${hashedName} 未变化 (hash=${e.hash})`);
  }
}

// 3. 更新全部 HTML 引用：固定名 → 哈希名
const files = htmlFiles();
let refCount = 0;
for (const e of entries) {
  const hashedName = e.src.replace(/(\.\w+)$/, `.${e.hash}$1`);
  refCount += replaceInHtml(files, `assets/${e.src}`, `assets/${hashedName}`);
}
console.log(`  HTML 引用更新: ${refCount} 处`);

// 4. 幂等校验：重跑内容哈希应不变
for (const e of entries) {
  const rehash = contentHash(e.buf);
  if (rehash !== e.hash) {
    console.error(`  ERROR: ${e.src} 重跑哈希不一致 (${e.hash} → ${rehash})`);
    process.exit(1);
  }
}
console.log('  幂等校验通过');
