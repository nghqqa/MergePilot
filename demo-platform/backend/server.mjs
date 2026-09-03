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
      const result = await handle(req.method, pathname, url.searchParams, body);
      return sendJson(res, 200, result);
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
const data = getReplayData();
if (!data.integrity.all_ok) {
  console.error('[boot] WARNING: evidence SHA256SUMS drift detected:', JSON.stringify(data.integrity.dirs, null, 2));
}
if (!data.replay_integrity.ok) {
  console.error('[boot] FATAL: replay source refs missing:', data.replay_integrity.missing_source_refs);
  process.exit(1);
}
console.log(`[boot] replay integrity OK — pr1 ${data.replay_integrity.events_pr1} events, pr2 ${data.replay_integrity.events_pr2} events, ${data.replay_integrity.source_refs_checked} source refs verified`);
console.log(`[boot] evidence integrity: ${data.integrity.ok_files}/${data.integrity.total_files} files match SHA256SUMS`);

server.listen(PORT, HOST, () => {
  console.log(`MergePilot demo platform → http://${HOST}:${PORT}`);
  console.log(`  mode default: REPLAY — HISTORICAL VERIFIED RUN`);
  console.log(`  live probe: GET /api/modes?mode=live (unavailable sources reported honestly)`);
});
