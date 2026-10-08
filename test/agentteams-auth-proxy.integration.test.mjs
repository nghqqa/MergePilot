// test/agentteams-auth-proxy.integration.test.mjs — 认证代理集成回归（Docker 门控）。
//
// 以【虚构凭据】起一次性 nginx 代理 + 假上游，覆盖部署器真实路径：
//  * 配置加载：渲染产物无占位符残留、代理可启动；
//  * 认证拒绝：正确凭据 200 / 无凭据 401 / 伪造凭据 401 / 错误凭据 401；
//  * 上游改写：上游收到的 Authorization=虚构上游 token（入口凭据绝不透传）；
//  * 上游故障：上游下线后代理 502（fail-closed，不假成功）；
//  * 安全日志：docker logs 与访问日志均不含入口凭据/上游 token 字面值。
// 无 Docker 环境自动跳过（CI console-backend 门不含本文件；本地与有 Docker 环境运行）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const proxyDir = fileURLToPath(new URL('../deploy/agentteams-beta/auth-proxy/', import.meta.url));
const { deployProxy, rmProxy, DEFAULT_IMAGE } = require(`${proxyDir}deploy-auth-proxy.cjs`);

const ENTRY = 'fake' + crypto.randomBytes(28).toString('hex');      // 虚构入口凭据（64hex 形态）
const UPSTREAM = 'fake-upstream-jwt-' + crypto.randomBytes(24).toString('hex'); // 虚构上游 token

function docker(args, { ok = true } = {}) {
  const r = spawnSync('docker', args, { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
  if (ok && r.status !== 0) throw new Error(`docker ${args[0]} failed: ${(r.stderr || r.stdout || '').slice(0, 200)}`);
  return (r.stdout || '').trim();
}
function hasDocker() { return spawnSync('docker', ['info'], { encoding: 'utf8' }).status === 0; }

test('auth-proxy 集成：认证拒绝/上游改写/上游故障/安全日志', async (t) => {
  if (!hasDocker()) return t.skip('docker not available');
  const rand = crypto.randomBytes(4).toString('hex');
  const net = `atproxy-it-${rand}`;
  const proxyName = `atproxy-it-proxy-${rand}`;
  const stubName = `atproxy-it-stub-${rand}`;
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'atproxy-it-'));
  const entryFile = path.join(tmp, 'entry.txt');
  const upstreamFile = path.join(tmp, 'upstream.txt');
  fs.writeFileSync(entryFile, ENTRY + '\n');
  fs.writeFileSync(upstreamFile, UPSTREAM + '\n');

  const stubConf = path.join(tmp, 'stub');
  fs.mkdirSync(stubConf);
  fs.writeFileSync(path.join(stubConf, 'default.conf'),
    // 模板固定 proxy_pass 到 :8090（生产=ctrl 端口）——stub 必须监听同一端口
    'server { listen 8090; default_type application/json; return 200 \'{\\"auth\\":\\"$http_authorization\\"}\'; }\n');

  let summary = null;
  try {
    docker(['network', 'create', net]);
    // Docker Desktop（Windows）实测：create 返回后网络传播有竞态——立即用会
    // "network not found"/启动 128。轮询 inspect 就绪后再起容器。
    let netReady = false;
    for (let i = 0; i < 30; i++) {
      if (spawnSync('docker', ['network', 'inspect', net], { encoding: 'utf8' }).status === 0) { netReady = true; break; }
      await new Promise((r) => setTimeout(r, 300));
    }
    assert.equal(netReady, true, 'test network did not become ready');

    docker(['run', '-d', '--name', stubName, '--network', net, '--network-alias', 'atproxy-it-stub',
      '-v', `${stubConf}:/etc/nginx/conf.d:ro`, DEFAULT_IMAGE]);
    // 核验 stub 真正 running（启动失败会在链路就绪探针前暴露为恒 502）
    let stubUp = false;
    for (let i = 0; i < 30 && !stubUp; i++) {
      const st = docker(['inspect', stubName, '--format', '{{.State.Status}}'], { ok: false });
      if (st === 'running') stubUp = true;
      else if (i === 14) docker(['start', stubName], { ok: false });
      else await new Promise((r) => setTimeout(r, 300));
    }
    assert.equal(stubUp, true, 'stub upstream failed to start');

    summary = deployProxy({
      outDir: path.join(tmp, 'rendered'),
      entryCredentialFile: entryFile,
      upstreamTokenFile: upstreamFile,
      ctrlHost: 'atproxy-it-stub',
      network: net,
      name: proxyName,
      publish: '127.0.0.1::8091', // ephemeral 宿主端口
    });
    assert.ok(summary.hostPort, 'ephemeral host port must be resolved');
    assert.ok(!fs.readFileSync(summary.configPath, 'utf8').includes('@AT_PROXY_'), 'rendered config has no placeholder residue');

    const base = `http://127.0.0.1:${summary.hostPort}`;
    // 等代理+上游全链就绪：持正确凭据探到 200（401/连接失败都继续等）
    let ready = false;
    for (let i = 0; i < 50; i++) {
      const r = await fetch(base + '/api/v1/workers', { headers: { authorization: `Bearer ${ENTRY}` } }).catch(() => null);
      if (r && r.status === 200) { ready = true; break; }
      await new Promise((r2) => setTimeout(r2, 300));
    }
    assert.equal(ready, true, 'proxy+upstream chain did not become ready');

    // ① 认证拒绝矩阵 + ② 上游改写
    const good = await fetch(base + '/api/v1/workers', { headers: { authorization: `Bearer ${ENTRY}` } });
    assert.equal(good.status, 200, 'correct fictional entry credential must pass');
    const seen = await good.json();
    assert.equal(seen.auth, `Bearer ${UPSTREAM}`, 'upstream must receive the replacement token');
    assert.notEqual(seen.auth, `Bearer ${ENTRY}`, 'entry credential must never be forwarded');

    const noAuth = await fetch(base + '/api/v1/workers');
    assert.equal(noAuth.status, 401, 'missing credential must be rejected');
    const badAuth = await fetch(base + '/api/v1/workers', { headers: { authorization: `Bearer ${UPSTREAM}` } });
    assert.equal(badAuth.status, 401, 'upstream token presented at the entry must be rejected');
    const fakeAuth = await fetch(base + '/api/v1/workers', { headers: { authorization: 'Bearer definitely-fake-000000' } });
    assert.equal(fakeAuth.status, 401, 'forged credential must be rejected');

    // ③ 上游故障 → 502（不假成功）
    docker(['rm', '-f', stubName]);
    let upstreamGone = false;
    for (let i = 0; i < 10; i++) {
      const r = await fetch(base + '/api/v1/workers', { headers: { authorization: `Bearer ${ENTRY}` } }).catch(() => null);
      if (r && r.status === 502) { upstreamGone = true; break; }
      await new Promise((r2) => setTimeout(r2, 300));
    }
    assert.equal(upstreamGone, true, 'proxy must return 502 when upstream is down');

    // ④ 安全日志：入口凭据/上游 token 字面值不得出现在容器日志
    const logs = docker(['logs', proxyName], { ok: false });
    assert.ok(!logs.includes(ENTRY), 'entry credential must never appear in logs');
    assert.ok(!logs.includes(UPSTREAM), 'upstream token must never appear in logs');
  } finally {
    rmProxy(proxyName);
    try { docker(['rm', '-f', stubName], { ok: false }); } catch { /* 已删 */ }
    try { docker(['network', 'rm', net], { ok: false }); } catch { /* 已删 */ }
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
