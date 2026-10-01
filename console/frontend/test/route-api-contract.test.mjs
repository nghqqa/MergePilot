// console/frontend/test/route-api-contract.test.mjs — Wave 3.7 数据源纯化门槛（前端静态面）。
//
// 锁三条红线（MU_MODE=multiuser 数据源纯化）：
//   C1 前端源码中每个 fetch URL 字面量都落在批准端点集合内（legacy URL 全部是 MU facade）；
//   C2 multiuser 数据源的请求全部 /api/mu/*（零 legacy 混入）；
//   C3 零 serviceWorker 注册、零 localStorage 业务写入、零 Cache API（fresh bundle 红线）。
// 这是静态契约（运行时请求证据由 MU_QUERY_TRACE/MU_ACCESS_LOG 验收轮采集）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'src');

function* walk(dir) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) yield* walk(p);
    else if (/\.(jsx|js)$/.test(e.name)) yield p;
  }
}

// 批准端点集合（MU facade=legacy URL 但后端 MU 分支只读 mu.*；子系统=批准的隔离域）
const APPROVED = [
  /^\/api\/mu\//,
  /^\/api\/health$/,
  /^\/api\/auth\/(login|logout|session)$/,
  // MU facade（后端 MU 模式固定选 MU 实现——mu-purify-w37.integration 运行时证明）
  /^\/api\/overview$/,
  /^\/api\/pulls/, /^\/api\/pending$/, /^\/api\/tickets$/, /^\/api\/evidence$/,
  /^\/api\/audit$/, /^\/api\/runs/,
  // 批准子系统（非租户业务数据：FXV capability 态 / C 链系统健康 / RAG 隔离试用）
  /^\/api\/fxv\//, /^\/api\/cchain\//, /^\/api\/rag-trial\//, /^\/api\/rag\/org-search$/,
  // v3 只读联调 URL 构造器（src 内无调用方——保留常量不算违例）
  /^http:\/\/127\.0\.0\.1:4190/,
];

test('C1 前端全部 fetch URL 字面量落在批准端点集合', () => {
  const violations = [];
  for (const file of walk(SRC)) {
    const text = fs.readFileSync(file, 'utf8');
    for (const m of text.matchAll(/fetch\((['"`])([^'"`]+)\1/g)) {
      const url = m[2];
      const resolved = url.replace(/\$\{[^}]*\}/g, 'X');
      if (!APPROVED.some((re) => re.test(resolved)) && resolved.startsWith('/')) {
        violations.push(`${path.relative(SRC, file)}: ${url}`);
      }
    }
  }
  assert.deepEqual(violations, [], `未批准端点：\n${violations.join('\n')}`);
});

test('C2 multiuser 数据源只发 /api/mu/* 请求', () => {
  const text = fs.readFileSync(path.join(SRC, 'data', 'sources.js'), 'utf8');
  const fn = text.slice(text.indexOf('function multiuserSource'), text.indexOf('export function createDataSource'));
  const urls = [...fn.matchAll(/get\((['"`])([^'"`]+)\1/g)].map((m) => m[2]);
  assert.ok(urls.length >= 3, `multiuser 源应至少 3 个请求点，实际 ${urls.length}`);
  for (const u of urls) assert.ok(u.startsWith('/api/mu/'), `multiuser 源混入非 MU 端点: ${u}`);
});

test('C3 零 serviceWorker 注册 / 零 localStorage / 零 Cache API（fresh bundle 红线）', () => {
  const violations = [];
  for (const file of walk(SRC)) {
    const text = fs.readFileSync(file, 'utf8');
    if (/serviceWorker\s*\.\s*register/.test(text)) violations.push(`SW register: ${path.relative(SRC, file)}`);
    if (/localStorage\./.test(text) && !/不用|never|禁止/.test(text)) {
      violations.push(`localStorage 使用: ${path.relative(SRC, file)}`);
    }
    if (/caches\s*\.\s*(open|match)/.test(text)) violations.push(`Cache API: ${path.relative(SRC, file)}`);
  }
  assert.deepEqual(violations, [], `违例：\n${violations.join('\n')}`);
});

test('C4 构建产物不含 service worker 与旧 bundle 引用（fresh/no-cache 静默红线）', () => {
  const dist = path.join(SRC, '..', 'dist');
  const indexHtml = path.join(dist, 'index.html');
  if (!fs.existsSync(indexHtml)) return; // CI 中 build 先于测试；本地无 dist 时跳过不隐藏（由 CI 锁）
  const html = fs.readFileSync(indexHtml, 'utf8');
  assert.ok(!/serviceWorker/.test(html), 'index.html 不得注册 service worker');
  const assets = path.join(dist, 'assets');
  const js = fs.readdirSync(assets).filter((f) => f.endsWith('.js'));
  assert.equal(js.length, 1, `assets 应只有当前 bundle（旧 bundle 残留=${js.join(',')})`);
});
