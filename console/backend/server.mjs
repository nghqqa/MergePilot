// console/backend/server.mjs — MergePilot 管理控制台（V0）
//
// 零第三方依赖的只读 HTTP 服务：
// - /api/* 从锁定的真实历史证据包（snapshot 模式）读取运行数据；
// - 其余 GET 服务于 frontend/dist（SPA）；
// - 默认只监听 127.0.0.1，不提供任何写操作接口。
//
// 启动：node console/backend/server.mjs
// 环境变量：CONSOLE_PORT（默认 4730）、CONSOLE_HOST（默认 127.0.0.1）、
//           CONSOLE_EVIDENCE_ROOT（默认 <repo>/evidence）

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { listRunPacks, safeResolve, listPackFiles, verifyPack, parseSha256Sums, looksTextual } from './lib/pack.mjs';
import { buildRunRecord, buildRunDetail } from './lib/runs.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_EVIDENCE_ROOT =
  process.env.CONSOLE_EVIDENCE_ROOT || path.resolve(__dirname, '..', '..', 'evidence');
const DEFAULT_DIST_DIR = path.resolve(__dirname, '..', 'frontend', 'dist');
const PORT = Number(process.env.CONSOLE_PORT || 4730);
const HOST = process.env.CONSOLE_HOST || '127.0.0.1';
const TEXT_VIEW_LIMIT = 512 * 1024;

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.map': 'application/json',
  '.txt': 'text/plain; charset=utf-8',
};

function send(res, status, body, headers = {}) {
  const buf = typeof body === 'string' ? Buffer.from(body, 'utf8') : body;
  res.writeHead(status, { 'Content-Length': buf.length, ...headers });
  res.end(buf);
}

function sendJson(res, status, obj) {
  send(res, status, JSON.stringify(obj, null, 2), { 'Content-Type': 'application/json; charset=utf-8' });
}

function sendError(res, status, message) {
  sendJson(res, status, { error: { code: status, message } });
}

export function createConsole({ evidenceRoot = DEFAULT_EVIDENCE_ROOT, distDir = DEFAULT_DIST_DIR } = {}) {
  const packDirOf = (packId) => {
    if (!/^[\w.-]+$/.test(packId)) {
      const err = new Error('bad pack id');
      err.status = 400;
      throw err;
    }
    const dir = path.join(evidenceRoot, packId);
    if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) {
      const err = new Error(`unknown run pack: ${packId}`);
      err.status = 404;
      throw err;
    }
    return dir;
  };

  const apiHealth = () => {
    const packs = listRunPacks(evidenceRoot);
    const withSums = packs.filter((p) => fs.existsSync(path.join(p.dir, 'SHA256SUMS'))).length;
    return {
      ok: true,
      service: 'mergepilot-console',
      version: '0.1.0',
      data_mode: 'snapshot',
      data_mode_note: '真实历史运行证据包（锁定只读），非实时数据；live 模式未接入',
      live: { configured: false, note: '实时接口（服务器 PG github_deliveries / MinIO meta）未接入，见 INTEGRATION-REQUESTS.md' },
      evidence_root: evidenceRoot,
      runs: packs.length,
      packs_with_sums: withSums,
      started_at: new Date().toISOString(),
    };
  };

  const apiRuns = (query) => {
    const packs = listRunPacks(evidenceRoot);
    let items = packs.map((p) => buildRunRecord(p.pack_id, p.dir));
    const { repo, pr, execution, verdict, publish, q } = query;
    if (repo) items = items.filter((r) => (r.repo ?? '').toLowerCase().includes(repo.toLowerCase()));
    if (pr) items = items.filter((r) => String(r.pr_number ?? '') === String(pr));
    if (execution) items = items.filter((r) => (r.execution.status ?? '').toUpperCase() === execution.toUpperCase());
    if (verdict) items = items.filter((r) => (r.review.verdict ?? '').toUpperCase() === verdict.toUpperCase());
    if (publish) items = items.filter((r) => (r.publish.status ?? '') === publish);
    if (q) {
      const needle = q.toLowerCase();
      items = items.filter((r) =>
        [r.pack_id, r.run_id, r.repo, r.head_sha, String(r.pr_number ?? '')]
          .some((v) => (v ?? '').toLowerCase().includes(needle)));
    }
    items.sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''));
    const total = items.length;
    const limit = Math.min(Math.max(Number(query.limit) || 50, 1), 200);
    const offset = Math.max(Number(query.offset) || 0, 0);
    return {
      data_mode: 'snapshot',
      generated_at: new Date().toISOString(),
      total,
      limit,
      offset,
      items: items.slice(offset, offset + limit),
    };
  };

  const apiEvidenceContent = async (packId, relPath, res, { download = false } = {}) => {
    const packDir = packDirOf(packId);
    const abs = safeResolve(packDir, relPath);
    let stat;
    try {
      stat = fs.statSync(abs);
    } catch {
      return sendError(res, 404, `file not found in pack: ${relPath}`);
    }
    if (!stat.isFile()) return sendError(res, 400, 'not a file');

    const sums = parseSha256Sums(packDir);
    const sumsStatus = !sums ? 'no_sums' : sums.has(relPath) ? 'listed' : 'unlisted';

    if (download) {
      const buf = await fs.promises.readFile(abs);
      return send(res, 200, buf, {
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': `attachment; filename="${path.basename(abs).replace(/["\r\n]/g, '')}"`,
        'X-Pack-Id': packId,
        'X-Sums-Status': sumsStatus,
      });
    }

    const handle = await fs.promises.open(abs, 'r');
    try {
      const sample = Buffer.alloc(Math.min(TEXT_VIEW_LIMIT, stat.size));
      await handle.read(sample, 0, sample.length, 0);
      const truncated = stat.size > TEXT_VIEW_LIMIT;
      if (!looksTextual(sample)) {
        return sendJson(res, 200, {
          path: relPath,
          bytes: stat.size,
          encoding: 'binary',
          truncated: false,
          sums_status: sumsStatus,
          note: '二进制文件，请使用下载',
        });
      }
      return sendJson(res, 200, {
        path: relPath,
        bytes: stat.size,
        encoding: 'utf-8',
        truncated,
        text: sample.toString('utf8'),
        sums_status: sumsStatus,
      });
    } finally {
      await handle.close();
    }
  };

  const serveStatic = (p, res) => {
    if (!fs.existsSync(distDir)) {
      return send(res, 503, `<!doctype html><meta charset="utf-8"><title>MergePilot Console</title>
<body style="font-family:system-ui;padding:2rem">
<h1>前端未构建</h1>
<p>请先构建控制台前端：<code>cd console/frontend && npm install && npm run build</code></p>
<p>API 仍然可用：<a href="/api/health">/api/health</a></p></body>`, { 'Content-Type': 'text/html; charset=utf-8' });
    }
    let abs = p === '/' ? path.join(distDir, 'index.html') : path.resolve(distDir, '.' + p);
    const rootAbs = path.resolve(distDir);
    if (abs !== path.join(rootAbs, 'index.html') && !abs.startsWith(rootAbs + path.sep)) {
      return sendError(res, 400, 'bad path');
    }
    try {
      if (!fs.statSync(abs).isFile()) throw new Error('not file');
    } catch {
      abs = path.join(distDir, 'index.html'); // SPA fallback
    }
    const ext = path.extname(abs).toLowerCase();
    const buf = fs.readFileSync(abs);
    return send(res, 200, buf, { 'Content-Type': MIME[ext] ?? 'application/octet-stream' });
  };

  const route = async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`);
    const p = url.pathname;
    const q = Object.fromEntries(url.searchParams.entries());

    if (!p.startsWith('/api/')) return serveStatic(p, res);

    if (p === '/api/health') return sendJson(res, 200, apiHealth());
    if (p === '/api/runs') return sendJson(res, 200, apiRuns(q));

    const runMatch = p.match(/^\/api\/runs\/([\w.-]+)(?:\/(.*))?$/);
    if (runMatch) {
      const [, packId, sub] = runMatch;
      if (!sub) return sendJson(res, 200, buildRunDetail(packId, packDirOf(packId)));
      if (sub === 'evidence') {
        const dir = packDirOf(packId);
        const sums = parseSha256Sums(dir);
        const files = listPackFiles(dir).map((f) => ({
          ...f,
          sums_status: !sums ? 'no_sums' : sums.has(f.path) ? 'listed' : 'unlisted',
        }));
        return sendJson(res, 200, { data_mode: 'snapshot', pack_id: packId, items: files });
      }
      if (sub === 'integrity') return sendJson(res, 200, await verifyPack(packId, packDirOf(packId)));
      if (sub === 'evidence/content') return apiEvidenceContent(packId, q.path, res);
      if (sub === 'evidence/download') return apiEvidenceContent(packId, q.path, res, { download: true });
      return sendError(res, 404, `unknown api path: ${p}`);
    }
    return sendError(res, 404, `unknown api path: ${p}`);
  };

  const server = http.createServer((req, res) => {
    Promise.resolve(route(req, res)).catch((e) => {
      try {
        sendError(res, e.status ?? 500, e.message ?? String(e));
      } catch { /* headers sent — nothing more we can do */ }
    });
  });

  return { server, route, apiHealth, apiRuns, evidenceRoot };
}

const isMain = process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href;
if (isMain) {
  const { server } = createConsole();
  server.listen(PORT, HOST, () => {
    console.log(`[console] MergePilot 管理控制台 V0`);
    console.log(`[console] 数据模式: snapshot（真实历史证据包，只读）`);
    console.log(`[console] evidence root: ${DEFAULT_EVIDENCE_ROOT}`);
    console.log(`[console] listening: http://${HOST}:${PORT} （仅本地回环）`);
  });
}
