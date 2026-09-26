// backend/server.mjs
// MergePilot Demo Backend — zero-dependency Node HTTP server.
//   - serves the built frontend (frontend/dist)
//   - exposes /api/* (all responses redacted)
//   - static assets with no-cache for API, short cache for assets

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { handle, redact } from './lib/api.mjs';
import { getReplayData } from '../evidence-adapter/replay-provider.mjs';
import { evidenceStatus } from '../evidence-adapter/evidence.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const DIST = path.resolve(__dirname, '..', 'frontend', 'dist');
const PORT = Number(process.env.DEMO_PORT || 4173);
const HOST = process.env.DEMO_HOST || '127.0.0.1';

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
};

function sendJson(res, status, obj) {
  const body = JSON.stringify(redact(obj), null, 2);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-demo-platform-mode': 'replay-primary',
  });
  res.end(body);
}

function serveStatic(req, res, pathname) {
  let rel = pathname === '/' ? '/index.html' : pathname;
  const abs = path.normalize(path.join(DIST, rel));
  if (!abs.startsWith(DIST)) {
    res.writeHead(403);
    return res.end('forbidden');
  }
  const candidate = fs.existsSync(abs) && fs.statSync(abs).isFile() ? abs : path.join(DIST, 'index.html'); // SPA fallback
  if (!fs.existsSync(candidate)) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    return res.end('frontend not built — run: npm run build:frontend');
  }
  const ext = path.extname(candidate).toLowerCase();
  res.writeHead(200, {
    'content-type': MIME[ext] || 'application/octet-stream',
    'cache-control': candidate.endsWith('index.html') ? 'no-store' : 'public, max-age=300',
  });
  fs.createReadStream(candidate).pipe(res);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = decodeURIComponent(url.pathname);
  try {
    if (pathname.startsWith('/api/')) {
      let body = null;
      if (req.method === 'POST') {
        const chunks = [];
        for await (const c of req) chunks.push(c);
        const raw = Buffer.concat(chunks).toString('utf8');
        try {
          body = raw ? JSON.parse(raw) : null;
        } catch {
          return sendJson(res, 400, { error: 'INVALID_JSON', detail: 'request body must be JSON' });
        }
      }
      // strip authorization-ish headers from any logging/echo surface; never echo them
      try {
        const result = await handle(req.method, pathname, url.searchParams, body);
        return sendJson(res, 200, result);
      } catch (e) {
        // fallback: a static-mode build calls baked endpoints as /api/**.json —
        // serve the baked file so the local server works with either bundle.
        if (req.method === 'GET' && pathname.endsWith('.json') && (e.status === 404 || e.status === 400)) {
          const abs = path.normalize(path.join(DIST, pathname));
          if (abs.startsWith(DIST) && fs.existsSync(abs) && fs.statSync(abs).isFile()) {
            res.writeHead(200, { 'content-type': MIME['.json'], 'cache-control': 'no-store' });
            return fs.createReadStream(abs).pipe(res);
          }
        }
        throw e;
      }
    }
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      return sendJson(res, 405, { error: 'METHOD_NOT_ALLOWED', detail: 'only GET/HEAD outside /api' });
    }
    return serveStatic(req, res, pathname);
  } catch (e) {
    const status = e.status || 500;
    return sendJson(res, status, { error: e.body?.error || 'INTERNAL_ERROR', detail: e.body?.detail || String(e && e.message || e) });
  }
});

// startup self-checks: evidence integrity + replay source integrity
// 2026-09-26 公开 checkout（无 evidence 包）以降级模式启动；API 层给出诚实空态。
const bootEvidence = evidenceStatus();
let data = null;
if (!bootEvidence.available) {
  console.warn(`[boot] DEGRADED MODE — evidence packs not distributed (${bootEvidence.reason}); missing: ${bootEvidence.missing.join(', ')}`);
  console.warn('[boot] replay API returns honest empty/503; /api/health reports degraded status.');
} else {
data = getReplayData();
if (!data.integrity.all_ok) {
  console.error('[boot] WARNING: evidence SHA256SUMS drift detected:', JSON.stringify(data.integrity.dirs, null, 2));
}
if (!data.replay_integrity.ok) {
  console.error('[boot] FATAL: replay source refs missing:', data.replay_integrity.missing_source_refs);
  process.exit(1);
}
console.log(`[boot] replay integrity OK — pr2 ${data.replay_integrity.events_pr2} events, pr3 ${data.replay_integrity.events_pr3} events, ${data.replay_integrity.source_refs_checked} source refs verified`);
console.log(`[boot] evidence integrity: ${data.integrity.ok_files}/${data.integrity.total_files} files match SHA256SUMS`);
}

// Windows（Hyper-V/WSL NAT）常保留大段端口，4173 落在保留段时报 EACCES。
// 策略：从 DEMO_PORT 起连续顺延最多 DEMO_PORT_TRIES-1 个端口；仍失败则交给
// 系统分配（port 0），控制台大声打印实际访问地址。
// 注意：listen(port, cb) 每次调用都会累积一个 once('listening') 监听，重试
// 链最终成功时会全部触发——成功回调必须只注册一次，错误按 bind 去重。
const PORT_TRIES = Number(process.env.DEMO_PORT_TRIES || 6);
let portOffset = 0;   // -1 表示已改用系统分配端口
let pendingBind = false;
server.once('listening', () => {
  pendingBind = false;
  const actual = server.address().port;
  const suffix = actual === PORT ? '' : `（默认端口 ${PORT} 被系统保留或占用，已自动改用 ${actual}）`;
  console.log(`MergePilot demo platform → http://${HOST}:${actual}${suffix}`);
  console.log(`  mode default: REPLAY — HISTORICAL VERIFIED RUN`);
  console.log(`  live probe: GET /api/modes?mode=live (unavailable sources reported honestly)`);
});
server.on('error', (err) => {
  if (!pendingBind) return; // 同一次 bind 的重复错误 / 迟到事件：忽略
  pendingBind = false;
  const manual = err.code === 'EACCES' || err.code === 'EADDRINUSE';
  if (manual && portOffset !== -1 && portOffset + 1 < PORT_TRIES) {
    portOffset += 1;
    console.warn(`[boot] ${err.code} on port ${PORT + portOffset - 1} - retrying on ${PORT + portOffset} ...`);
    bindServer();
    return;
  }
  if (manual && portOffset !== -1) {
    portOffset = -1;
    console.warn(`[boot] 连续 ${PORT_TRIES} 个端口均被系统保留或占用 - 交由系统分配可用端口`);
    bindServer();
    return;
  }
  if (err.code === 'EACCES') {
    console.error('[boot] 端口被系统拒绝：Windows 保留端口段常见于 Hyper-V/WSL（查看：netsh interface ipv4 show excludedportrange protocol=tcp）');
    console.error('[boot] 也可指定起始端口后重试：set DEMO_PORT=5050 && start-demo.bat');
  }
  console.error('[boot] listen failed:', err.message);
  process.exit(1);
});
function bindServer() {
  pendingBind = true;
  const port = portOffset === -1 ? 0 : PORT + portOffset;
  server.listen(port, HOST);
}
bindServer();
