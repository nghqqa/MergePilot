// contract-fixture-harness.mjs — 开发/测试专用的契约 v2 fixture harness（非业务后端）。
//
// 用途：让真实页面经过真实适配层（api-live.js / data/sources.js）读取正式契约形状的响应，
// 完成"页面级契约验收"。它不是新增业务后端：
//   - 不实现存储、授权策略或审批状态机（数据全部来自同目录 contract-harness-data.json，合成）；
//   - 不打包进生产构建、不作为生产服务启用（NODE_ENV=production 拒绝启动）；
//   - 所有响应带 data_mode:"fixture"，页面全程可见 fixture 标识。
//
// 运行：node dev/contract-fixture-harness.mjs [--port 4192] [--dist ../dist]
//
// 端点（契约 v2 @ 7ccecb9 的形状；仅 GET + 静态资源）：
//   GET /api/health              → sources.primary='contract'（可信配置声明数据源）
//   GET /api/auth/session        → 200 fixture 用户（合成；非真实登录）
//   GET /api/me/capabilities     → 能力（request_merge: merge_disabled）
//   GET /api/pulls?repo=         → PR 列表（§2 形状）
//   GET /api/pulls/:n?repo=      → PR 详情（runs/merge_panel/patch_delivery）
//   GET /fixtures/*              → 合成补丁文件
//   其余 GET                     → dist 静态资源 + SPA fallback

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

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.map': 'application/json',
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

const health = () => ({
  ok: true,
  service: 'mp-contract-fixture-harness',
  data_mode: 'fixture',
  data_mode_note: 'DEV/TEST ONLY — 全部数据为合成 fixture，非真实运行、非历史快照；无存储/授权/审批状态机',
  sources: { primary: 'contract', contract_v2: { available: true, data_mode: 'fixture' } },
  declared_repos: DATA.repos,
  note: '这是开发/测试 harness：模式由本配置声明，前端不因 sessionStorage/URL 获得 contract 权限',
});

const server = http.createServer((req, res) => {
  const u = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const p = u.pathname;
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    return sendJson(res, 405, { error: { code: 405, reason: 'read_only', message: 'harness 仅提供 GET（测试只读面）' } });
  }

  if (p === '/api/health') return sendJson(res, 200, health());

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

  // 静态 dist + SPA fallback
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
  console.log(`[harness] contract fixture harness on http://127.0.0.1:${PORT}  dist=${DIST}`);
  console.log('[harness] DEV/TEST ONLY — 合成数据；无存储/授权/审批状态机；不作为生产服务');
});
