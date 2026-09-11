// rag-forwarder.mjs — read-only bridge so the CoPaw worker containers (docker
// network) can reach the loopback-only demo backend's RAG endpoints.
// Exposes ONLY /api/rag/* (GET for search/dataset, POST for tool-span audit),
// forwarding to 127.0.0.1:4173. No other paths, no caching, no auth material.
import http from 'node:http';

const PORT = Number(process.env.RAG_FWD_PORT || 4174);
const UPSTREAM_HOST = '127.0.0.1';
const UPSTREAM_PORT = 4173;

const server = http.createServer((req, res) => {
  if (!req.url.startsWith('/api/rag/') && !req.url.startsWith('/api/db/')) {
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'NOT_FOUND', detail: 'forwarder exposes /api/rag/* and /api/db/* only' }));
    return;
  }
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const body = Buffer.concat(chunks);
    const up = http.request(
      { host: UPSTREAM_HOST, port: UPSTREAM_PORT, path: req.url, method: req.method,
        headers: { 'content-type': 'application/json', 'content-length': body.length } },
      (ur) => {
        res.writeHead(ur.statusCode, { 'content-type': ur.headers['content-type'] || 'application/json' });
        ur.pipe(res);
      });
    up.on('error', (e) => {
      res.writeHead(502, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: 'UPSTREAM_ERROR', detail: String(e) }));
    });
    up.end(body);
  });
});

server.listen(PORT, '0.0.0.0', () => console.log(`rag-forwarder listening 0.0.0.0:${PORT} -> 127.0.0.1:4173`));
