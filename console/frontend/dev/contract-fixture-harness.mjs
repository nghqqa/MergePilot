// contract-fixture-harness.mjs — 开发/测试专用 harness（非业务后端）。
//
// 两种模式：
// 1) 契约 fixture 页面验收（默认）：
//    GET /api/health → sources.primary='contract'；/api/auth/session → 200 合成用户；
//    /api/me/capabilities、/api/pulls、/api/pulls/:n 按 API-AUTH-MERGE-V0 §1/§2 形状；
//    数据来自同目录 contract-harness-data.json（合成）。
// 2) PG 联调模式（--pg http://127.0.0.1:4193）：
//    /api/health → sources.primary='console-pg'（可信配置声明）；
//    /api/auth/session 与 /pg/* 反向代理到隔离 console_pg 只读服务（透传 401/503 如实）。
//
// 共同边界：不实现存储、授权策略或审批状态机；不打包进生产构建；
// NODE_ENV=production 拒绝启动；所有响应带 X-Data-Mode: fixture。

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';

if (process.env.NODE_ENV === 'production') {
  console.error('[harness] 拒绝启动：NODE_ENV=production。此 harness 仅用于开发/测试环境。');
  process.exit(1);
}

const argv = process.argv.slice(2);
const arg = (name, dflt) => {
  const i = argv.indexOf(name);
  return i >= 0 ? argv[i + 1] : dflt;
};
const PORT = Number(arg('--port', '4192'));
const DIST = path.resolve(path.dirname(url.fileURLToPath(import.meta.url)), arg('--dist', '../dist'));
const DATA = JSON.parse(fs.readFileSync(
  path.resolve(path.dirname(url.fileURLToPath(import.meta.url)), 'contract-harness-data.json'), 'utf8'));
// --pg http://127.0.0.1:4193：PG 联调模式——/pg/* 反向代理到隔离 console_pg 只读服务，
// /api/health 声明数据源 console-pg（可信配置），/api/auth/session 透传（401 如实）。
// 仍是本 harness（开发/测试基础设施），不新增业务后端。
const PG_TARGET = arg('--pg', null);
const PROXYABLE = ['/api/auth/session'];

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.map': 'application/json',
  '.patch': 'text/plain; charset=utf-8',
};

const send = (res, code, body, ctype = 'application/json; charset=utf-8') => {
  const buf = typeof body === 'string' ? Buffer.from(body, 'utf8') : body;
  res.writeHead(code, {
    'Content-Type': ctype,
    'Content-Length': buf.length,
    'Cache-Control': 'no-store',
    'X-Data-Mode': 'fixture',
  });
  res.end(buf);
};
const sendJson = (res, code, obj) => send(res, code, JSON.stringify(obj, null, 2));

const healthContract = () => ({
  ok: true,
  service: 'mp-contract-fixture-harness',
  data_mode: 'fixture',
  data_mode_note: 'DEV/TEST ONLY — 全部数据为合成 fixture，非真实运行、非历史快照；无存储/授权/审批状态机',
  sources: { primary: 'contract', contract_v2: { available: true, data_mode: 'fixture' } },
  declared_repos: DATA.repos,
  note: '这是开发/测试 harness：模式由本配置声明，前端不因 sessionStorage/URL 获得 contract 权限',
});
const healthPg = (target) => ({
  ok: true,
  service: 'mp-dev-harness(pg)',
  data_mode: 'fixture',
  data_mode_note: 'DEV/TEST ONLY — 数据来自隔离 PG 只读服务（fixture 测试记录），非真实运行、非历史快照',
  sources: { primary: 'console-pg', console_pg: { available: true, base: '/pg' } },
  note: `开发/测试 harness：/pg/* 反向代理至隔离 console_pg（${target}）；模式由本配置声明`,
});

async function proxy(res, target, reqPath, search, { method = 'GET', req } = {}) {
  try {
    const init = {
      method,
      headers: { Accept: 'application/json' },
      signal: AbortSignal.timeout(10000),
    };
    // 审批决策为 POST：透传请求体（隔离后端自身强制只读/主体校验；harness 无业务逻辑）
    if (method === 'POST' && req) {
      const chunks = [];
      for await (const ch of req) chunks.push(ch);
      const raw = Buffer.concat(chunks);
      if (raw.length) {
        init.body = raw;
        init.headers['Content-Type'] = req.headers['content-type'] ?? 'application/json; charset=utf-8';
      }
      if (req.headers['x-test-principal']) {
        init.headers['X-Test-Principal'] = req.headers['x-test-principal'];
      }
    }
    const upstream = await fetch(`${target.replace(/\/$/, '')}${reqPath}${search}`, init);
    const body = Buffer.from(await upstream.arrayBuffer());
    res.writeHead(upstream.status, {
      'Content-Type': upstream.headers.get('content-type') ?? 'application/json; charset=utf-8',
      'Content-Length': body.length,
      'Cache-Control': 'no-store',
      'X-Data-Mode': 'fixture',
    });
    res.end(body);
  } catch (e) {
    sendJson(res, 502, { error: { code: 502, reason: 'proxy_target_unreachable', message: String(e).slice(0, 160) } });
  }
}

const server = http.createServer((req, res) => {
  const u = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const p = u.pathname;
  const isReadVerb = req.method === 'GET' || req.method === 'HEAD';
  const isProxyablePost = req.method === 'POST' && PG_TARGET &&
    (p === '/api/auth/session' || p === '/pg' || p.startsWith('/pg/'));
  if (!isReadVerb && !isProxyablePost) {
    return sendJson(res, 405, { error: { code: 405, reason: 'read_only', message: 'harness 测试只读面（仅 /pg 代理可透传 POST）' } });
  }

  // ---- PG 联调模式（--pg）----
  if (PG_TARGET) {
    if (p === '/api/health') return sendJson(res, 200, healthPg(PG_TARGET));
    if (p === '/api/auth/session' || p === '/pg' || p.startsWith('/pg/')) {
      const rest = p.startsWith('/pg/') ? p.slice(3) : p;
      return proxy(res, PG_TARGET, rest || '/', u.search, { method: req.method, req });
    }
    if (p.startsWith('/api/')) {
      return sendJson(res, 404, { error: { code: 404, reason: 'not_in_pg_mode', message: 'PG 联调模式：契约 fixture 端点未启用（数据走 /pg 代理）' } });
    }
  } else {
    // ---- 契约 fixture 页面验收模式（默认）----
    if (p === '/api/health') return sendJson(res, 200, healthContract());

    if (p === '/api/auth/session') {
      // fixture 用户（合成）——页面顶栏将显示"Fixture 验收会话 · 非真实"
      return sendJson(res, 200, {
        user: DATA.user,
        expires_at: '2026-12-31T00:00:00Z',
        capabilities_version: 1,
        data_mode: 'fixture',
      });
    }

    if (p === '/api/me/capabilities') {
      const repo = u.searchParams.get('repo') ?? '';
      return sendJson(res, 200, {
        repo,
        installation_state: 'both',
        data_mode: 'fixture',
        operations: {
          approve_tickets: { allowed: false, reason: 'approvals_not_implemented' },
          request_merge: {
            allowed: false, reason: 'merge_disabled',
            detail: '站内合并未启用——使用 GitHub 原生合并',
            github_url: repo ? `https://github.com/${repo}/pulls` : null,
          },
        },
      });
    }

    if (p === '/api/pulls') {
      const repo = u.searchParams.get('repo') ?? '';
      const items = DATA.pulls[repo];
      if (!items) {
        return sendJson(res, 404, { error: { code: 404, reason: 'repo_not_found', message: `harness 无 ${repo} 的 fixture 数据` } });
      }
      return sendJson(res, 200, { data_mode: 'fixture', total: items.length, items });
    }

    const pullsMatch = p.match(/^\/api\/pulls\/(\d+)$/);
    if (pullsMatch) {
      const repo = u.searchParams.get('repo') ?? '';
      const n = Number(pullsMatch[1]);
      const detail = DATA.details[`${repo}#${n}`];
      if (!detail) {
        return sendJson(res, 404, { error: { code: 404, reason: 'pr_not_found', message: `harness 无 ${repo}#${n} 的 fixture 详情` } });
      }
      return sendJson(res, 200, {
        repo, pr_number: n, state: 'open', data_mode: 'fixture',
        // 契约语义：current_head_sha 是 GitHub 权威当前 head，与 latest_result 的 head 无关
        current_head_sha: detail.current_head_sha ?? null,
        title: (DATA.pulls[repo] ?? []).find((x) => x.pr_number === n)?.title ?? null,
        ...detail,
      });
    }

    if (p.startsWith('/fixtures/')) {
      const body = DATA.patch_files[p];
      if (body == null) return sendJson(res, 404, { error: { code: 404, reason: 'not_found', message: 'no such fixture file' } });
      return send(res, 200, body, 'text/plain; charset=utf-8');
    }
  }

  // 静态 dist + SPA fallback（两种模式共用）
  const rel = p === '/' ? '/index.html' : p;
  const abs = path.resolve(DIST, `.${path.posix.normalize(rel)}`);
  if (abs.startsWith(DIST) && fs.existsSync(abs) && fs.statSync(abs).isFile()) {
    return send(res, 200, fs.readFileSync(abs), MIME[path.extname(abs)] ?? 'application/octet-stream');
  }
  const index = path.join(DIST, 'index.html');
  if (fs.existsSync(index)) return send(res, 200, fs.readFileSync(index), 'text/html; charset=utf-8');
  return sendJson(res, 404, { error: { code: 404, reason: 'not_found', message: 'dist 未构建：先在 console/frontend 执行 npm run build' } });
});

server.listen(PORT, '127.0.0.1', () => {
  const mode = PG_TARGET ? `PG 联调（代理 → ${PG_TARGET}）` : '契约 fixture 页面验收';
  console.log(`[harness] ${mode} on http://127.0.0.1:${PORT}  dist=${DIST}`);
  console.log('[harness] DEV/TEST ONLY — 合成/隔离数据；无存储/授权/审批状态机；不作为生产服务');
});
