// Wave 3.9 二A：webhook-only ingress——隧道只暴露 GitHub webhook 与 health，
// 管理面（/multiuser、其余 /api/*）一律 404（本机回环访问不受影响）。
// rc.14 自托管适配：上游改为 compose internal 网络内的 console 服务直连。
const UPSTREAM_HOST = process.env.UPSTREAM_HOST || 'console';
const UPSTREAM_PORT = Number(process.env.UPSTREAM_PORT || 4730);
const http = require('http');
const ALLOW = [
  ['POST', /^\/api\/mu\/github\/webhook$/],
  ['GET', /^\/api\/health$/],
];
http.createServer((req, res) => {
  const path = req.url.split('?')[0];
  const ok = ALLOW.some(([m, re]) => req.method === m && re.test(path));
  if (!ok) {
    res.writeHead(404, { 'content-type': 'application/json' });
    return res.end('{"error":{"reason":"path_not_exposed"}}');
  }
  const preq = http.request({
    host: UPSTREAM_HOST, port: UPSTREAM_PORT, path: req.url, method: req.method,
    headers: { ...req.headers, host: `${UPSTREAM_HOST}:${UPSTREAM_PORT}` },
  }, (pres) => { res.writeHead(pres.statusCode, pres.headers); pres.pipe(res); });
  preq.on('error', () => { res.writeHead(502, {'content-type':'application/json'}); res.end('{"error":{"reason":"upstream_unreachable"}}'); });
  req.pipe(preq);
}).listen(8080, () => console.log('webhook-only proxy up on 8080'));
