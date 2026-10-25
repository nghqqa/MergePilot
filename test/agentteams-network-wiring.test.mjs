// test/agentteams-network-wiring.test.mjs — console↔agentteams 声明化接线回归（CI 门内）。
// 背景：2026-10-25 波实证 compose 重建丢失手工 network connect（gitee_unreachable /
// executor_gate_rejected 两处 fail-closed 拦截实录）——接线必须声明化，重建自动保持。
// 覆盖：叠加文件契约（external 网络+console 双网络）/ 生产配方零 fake-gitee /
// 入口↔wire 脚本锁步（含认证口径=cli-token 即读即用，无铸 JWT）/ env 幂等合并行为 /
// docker compose 渲染（有 docker 处执行；本地 Windows Docker Desktop 停止时如实跳过）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(HERE, '..');
const SH = path.join(ROOT, 'deploy', 'selfhost');
const AT = path.join(ROOT, 'deploy', 'agentteams', 'qwenpaw-v124');
const ovSrc = fs.readFileSync(path.join(SH, 'docker-compose.agentteams.yml'), 'utf8');
const baseSrc = fs.readFileSync(path.join(SH, 'docker-compose.yml'), 'utf8');
const entrySrc = fs.readFileSync(path.join(AT, 'install-agentteams-full.sh'), 'utf8');

test('叠加文件契约：console 双网络 + external 网络名可注入', () => {
  const flat = ovSrc.replace(/\n\s*/g, ' ');
  assert.ok(flat.includes('networks: [internal, agentteams]'), 'console 必须同时接 internal+agentteams');
  assert.ok(ovSrc.includes('external: true'), '执行器网络必须声明 external（由 apply 创建）');
  assert.ok(ovSrc.includes('${MU_AGENTTEAMS_NETWORK:-'), '网络名必须可由 env 注入');
});

test('生产配方边界：selfhost 基础 compose 不含 agentteams 接线与 fake-gitee', () => {
  assert.ok(!baseSrc.includes('agentteams'), '基础 compose 不得含 agentteams（无执行器部署不应要求该网络存在）');
  assert.ok(!baseSrc.includes('fake-gitee'), 'fake-gitee 属隔离验收件，禁止混入生产配方');
  assert.ok(!ovSrc.includes('fake-gitee'), '叠加文件同样不得含 fake-gitee');
});

test('入口锁步：接线经 wire 脚本+网络名+叠加命令；认证口径=cli-token 即读即用（无铸 JWT 声称）', () => {
  assert.ok(entrySrc.includes('wire-console-env.sh'), '入口必须经 wire 脚本合并栈 env');
  assert.ok(entrySrc.includes('MU_AGENTTEAMS_NETWORK=$QP_NETWORK'), '入口必须写入执行器网络名');
  assert.ok(entrySrc.includes('docker-compose.agentteams.yml'), '入口必须提示叠加文件重建命令');
  assert.ok(!entrySrc.includes('docker network connect'), '入口不得再走手工 network connect（声明化替代）');
  assert.ok(!entrySrc.includes('铸长效 SA JWT'), '认证注释不得再声称铸 JWT（实际=cli-token 即读即用）');
  assert.ok(entrySrc.includes('不自铸、不延有效期、不关认证'), '认证边界声明必须在场');
});

test('wire 行为：幂等合并（同键覆盖单份/无关行保留/0600）（POSIX；CI ubuntu 执行）',
  { skip: process.platform === 'win32' ? 'POSIX 权限位语义——CI ubuntu 执行' : false }, () => {
    const sb = fs.mkdtempSync(path.join(os.tmpdir(), 'at-wire-test-'));
    const wire = path.join(sb, 'wire.env');
    fs.writeFileSync(wire, [
      '# console 执行器接线 env',
      'MU_EXECUTOR=agentteams',
      'MU_AGENTTEAMS_TOKEN=tok-value-1',
    ].join('\n') + '\n');
    const envf = path.join(sb, 'stack.env');
    fs.writeFileSync(envf, [
      'CONSOLE_PORT=4730',
      'MU_AGENTTEAMS_TOKEN=stale-old-value',
      'POSTGRES_PASSWORD=pg-pass',
    ].join('\n') + '\n', { mode: 0o644 });
    const script = path.join(AT, 'wire-console-env.sh');
    execFileSync('bash', [script, wire, envf, 'MU_AGENTTEAMS_NETWORK=qwp2-net'], { encoding: 'utf8' });
    execFileSync('bash', [script, wire, envf, 'MU_AGENTTEAMS_NETWORK=qwp2-net'], { encoding: 'utf8' }); // 重复执行=幂等
    const lines = fs.readFileSync(envf, 'utf8').split('\n').filter(Boolean);
    const count = (k) => lines.filter((l) => l.startsWith(k + '=')).length;
    assert.equal(count('MU_AGENTTEAMS_TOKEN'), 1, '同键合并后必须仅一份');
    assert.ok(lines.includes('MU_AGENTTEAMS_TOKEN=tok-value-1'), 'wire 值必须覆盖栈内旧值');
    assert.equal(count('MU_AGENTTEAMS_NETWORK'), 1, '直通键幂等单份');
    assert.ok(lines.includes('CONSOLE_PORT=4730') && lines.includes('POSTGRES_PASSWORD=pg-pass'), '无关行必须原样保留');
    assert.equal(fs.statSync(envf).mode & 0o777, 0o600, 'env 文件必须保持 0600');
  });

test('compose 渲染：叠加后 console 含注入的 agentteams 网络；基础渲染不含（有 docker 处执行）',
  { skip: hasDocker() ? false : 'docker 不可达——CI 由 docker info 硬门覆盖' }, () => {
    const sb = fs.mkdtempSync(path.join(os.tmpdir(), 'at-render-test-'));
    for (const f of ['docker-compose.yml', 'docker-compose.agentteams.yml']) {
      fs.copyFileSync(path.join(SH, f), path.join(sb, f));
    }
    fs.writeFileSync(path.join(sb, '.env'), 'POSTGRES_PASSWORD=render-test-not-a-secret\n'); // env_file 存在+必填插值哑值（config 渲染不校验强度）
    const out = execFileSync('docker',
      ['compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.agentteams.yml', 'config'],
      { cwd: sb, encoding: 'utf8', env: { ...process.env, MU_AGENTTEAMS_NETWORK: 'wiring-test-net' } });
    assert.ok(out.includes('wiring-test-net'), '渲染必须使用注入的网络名');
    assert.ok(/agentteams/.test(out), '叠加渲染必须含 agentteams 网络');
    const outBase = execFileSync('docker', ['compose', '-f', 'docker-compose.yml', 'config'],
      { cwd: sb, encoding: 'utf8' });
    assert.ok(!outBase.includes('agentteams'), '基础渲染不得含 agentteams 网络');
  });

function hasDocker() {
  try {
    execFileSync('docker', ['info', '--format', '{{.ServerVersion}}'], { encoding: 'utf8', timeout: 15000 });
    return true;
  } catch { return false; }
}
