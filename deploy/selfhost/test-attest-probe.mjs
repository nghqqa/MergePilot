#!/usr/bin/env node
// provider attestation 探测（probeAttest）八场景隔离自测 — 零依赖，node >= 20，无需 Docker。
// 用法：node test-attest-probe.mjs
// 场景：正常 200 / 端点不可达 / HTTP 非 200（文件缺失 404）/ 内容非 JSON / 形状校验失败 /
//       内容不一致（与本地源文件比对）/ 本地源文件缺失 / 重启恢复（服务重启后再次 200）。
// 全部通过 exit 0；任一失败 exit 1。纪律：只读，不输出任何 secret。
import { createServer } from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { probeAttest } from './preflight.mjs';

const GOOD = JSON.stringify({
  provider: 'selfhost-test',
  model: 'test-model',
  attestation: { algo: 'static-file-v1' },
  key_id: 'k1',
});

// 进程内静态服务：mode 切换响应内容（good / badjson / emptyshape）；
// 重启复用同一端口（贴近真实静态服务重启）。
let mode = 'good';
let srv;
let port = 0;
async function startServer(fixedPort = 0) {
  srv = createServer((req, res) => {
    if (req.url === '/missing') { res.writeHead(404, { 'content-type': 'text/plain' }); res.end('not found'); return; }
    if (mode === 'badjson') { res.writeHead(200, { 'content-type': 'application/json' }); res.end('not-json{{{'); return; }
    if (mode === 'emptyshape') { res.writeHead(200, { 'content-type': 'application/json' }); res.end('{}'); return; }
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(GOOD);
  });
  await new Promise((r, j) => {
    srv.once('error', j);
    srv.listen(fixedPort, '127.0.0.1', () => { srv.removeAllListeners('error'); r(); });
  });
  port = srv.address().port;
}
async function stopServer() {
  srv.closeAllConnections?.();
  await new Promise((r) => srv.close(r));
  await new Promise((r) => setTimeout(r, 100));
}

await startServer();
const base = `http://127.0.0.1:${port}`;

// 本地源文件夹具：good.json（与端点一致）/ mismatch.json（不一致）
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-attest-probe-'));
fs.writeFileSync(path.join(tmp, 'good.json'), GOOD);
fs.writeFileSync(path.join(tmp, 'mismatch.json'), GOOD.replace('selfhost-test', 'other-node'));

const cases = [];
const check = async (name, urlPromise, expected, file) => {
  const url = await urlPromise;
  const r = await probeAttest(url, { file, timeoutMs: 3000 });
  cases.push({ name, expected, actual: r.outcome, detail: r.detail });
};

await check('1. 正常 200 + 形状通过', Promise.resolve(`${base}/attestation.json`), 'pass');
await check('2. 端点不可达（回环无监听）', Promise.resolve('http://127.0.0.1:1/attestation.json'), 'fail');
await check('3. HTTP 非 200（attestation.json 缺失→404）', Promise.resolve(`${base}/missing`), 'fail');

mode = 'badjson';
await check('4. 内容校验失败：响应非 JSON', Promise.resolve(`${base}/attestation.json`), 'fail');
mode = 'emptyshape';
await check('5. 内容校验失败：形状缺字段', Promise.resolve(`${base}/attestation.json`), 'fail');

mode = 'good';
await check('6. 内容不一致（端点 vs 本地源文件）', Promise.resolve(`${base}/attestation.json`), 'fail', path.join(tmp, 'mismatch.json'));
await check('7. 本地源文件缺失', Promise.resolve(`${base}/attestation.json`), 'fail', path.join(tmp, 'nope.json'));
await check('7b. 内容一致（源文件比对通过）', Promise.resolve(`${base}/attestation.json`), 'pass', path.join(tmp, 'good.json'));

// 8. 重启恢复：静态服务在原端口重启（模拟主机/守护重启）后端点回到 200
await stopServer();
await startServer(port);
await check('8. 重启恢复：服务重启后回到 200+形状通过', Promise.resolve(`${base}/attestation.json`), 'pass');

await stopServer();
fs.rmSync(tmp, { recursive: true, force: true });

let bad = 0;
for (const c of cases) {
  const pass = c.actual === c.expected;
  if (!pass) bad++;
  console.log(`${pass ? '  PASS  ' : '  FAIL  '}[${c.actual}，期望 ${c.expected}] ${c.name}${pass ? '' : '  — ' + c.detail}`);
}
console.log(`\n${cases.length - bad}/${cases.length} scenarios pass`);
// 留出句柄收尾时间，规避 Windows 下 libuv 关闭竞态断言
setTimeout(() => process.exit(bad ? 1 : 0), 200);
