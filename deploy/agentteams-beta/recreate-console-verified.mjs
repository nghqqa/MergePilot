#!/usr/bin/env node
// recreate-console-verified.mjs — 生产 console（beta-mp-console）受控重建（2026-10-21 matrixpw 事故固化）。
//
// 背景：2026-10-21 生产 console 以不完整 env 重建，MU_AGENTTEAMS_MATRIX_PASSWORD 缺失
// → 执行器门如实拒绝（MT_NOT_CONFIGURED）→ 修复轮被拦。本脚本是**唯一规范重建入口**：
// 变更前形状断言（必需 env 全非空才放行）→ 同镜像同参重建 → 变更后身份验证。
//
// 用法（锚点=变更前 docker inspect JSON，放 D:/goai/secrets/，不入版本库）：
//   node deploy/agentteams-beta/recreate-console-verified.mjs <anchor-inspect.json> [--execute]
//   不带 --execute 只做形状断言与计划输出（dry-run 默认）。
//
// 纪律：任何明文凭据不落日志/报告；密码真值必须来自受控秘密源的锚点文件（找回密码不是恢复流程）。
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const NAME = 'beta-mp-console';
const REQUIRED_ENV = [
  'MU_MODE', 'CONSOLE_PG_DSN', 'CONSOLE_SESSION_SECRET',
  'MU_AGENTTEAMS_BASE_URL', 'MU_AGENTTEAMS_TOKEN',
  'MU_AGENTTEAMS_MATRIX_URL', 'MU_AGENTTEAMS_MATRIX_USER', 'MU_AGENTTEAMS_MATRIX_PASSWORD',
  'MU_GITHUB_OAUTH_CLIENT_ID', 'MU_GITHUB_OAUTH_CLIENT_SECRET',
];
// 形状断言只需"非空"，不需要具体值——以下键永不打印值。
const ANCHOR = process.argv[2];
const EXECUTE = process.argv.includes('--execute');
if (!ANCHOR) { console.error('usage: recreate-console-verified.mjs <anchor-inspect.json> [--execute]'); process.exit(2); }

const load = (p) => { const j = JSON.parse(fs.readFileSync(p, 'utf8')); return Array.isArray(j) ? j[0] : j; };
const toMap = (a) => Object.fromEntries((a ?? []).map((x) => { const i = x.indexOf('='); return [x.slice(0, i), x.slice(i + 1)]; }));

const anchor = load(ANCHOR);
const env = toMap(anchor.Config.Env);
const missing = REQUIRED_ENV.filter((k) => !(env[k] && env[k].length > 0));
console.log('anchor:', ANCHOR);
console.log('image:', anchor.Image);
console.log('env key count:', Object.keys(env).length);
if (missing.length > 0) {
  console.error('SHAPE_ASSERT_FAILED — 以下必需 env 缺失或为空（拒绝重建，零变更）:');
  for (const k of missing) console.error('  -', k);
  console.error('先从受控秘密源补齐锚点再执行；不得跳过本断言。');
  process.exit(1);
}
console.log('shape assert: PASS（所有必需 env 非空）');

const ports = anchor.HostConfig.PortBindings ?? {};
const binds = anchor.HostConfig.Binds ?? [];
const restart = anchor.HostConfig.RestartPolicy?.Name === 'no' ? 'unless-stopped' : anchor.HostConfig.RestartPolicy?.Name;
const netAt = Object.entries(anchor.NetworkSettings.Networks).find(([n]) => n !== 'bridge');
console.log('plan: ports=' + JSON.stringify(Object.keys(ports)) + ' binds=' + binds.length + ' restart=' + restart + ' extra-net=' + (netAt?.[0] ?? 'none'));

if (!EXECUTE) { console.log('dry-run（加 --execute 执行）。'); process.exit(0); }

const args = ['run', '-d', '--name', NAME, '--restart', restart ?? 'unless-stopped'];
for (const [p, bindsArr] of Object.entries(ports)) {
  for (const b of bindsArr) args.push('-p', `${b.HostIp}:${b.HostPort}:${p}`);
}
for (const b of binds) args.push('-v', b);
for (const [k, v] of Object.entries(env)) args.push('-e', `${k}=${v}`);
args.push(anchor.Image);
execFileSync('docker', ['rm', '-f', NAME], { stdio: 'pipe' });
const id = execFileSync('docker', args, { stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
console.log('recreated:', id.slice(0, 12));
// bridge 为默认网络；非 bridge 网络按锚点原 IP 回接（别名按生产配方三别名）
for (const [net, cfg] of Object.entries(anchor.NetworkSettings.Networks)) {
  if (net === 'bridge') continue;
  const aliases = (cfg.Aliases ?? []).filter(Boolean);
  const aliasArgs = aliases.flatMap((a) => ['--alias', a]);
  const ipArgs = cfg.IPAddress ? ['--ip', cfg.IPAddress] : [];
  execFileSync('docker', ['network', 'connect', ...aliasArgs, ...ipArgs, net, NAME], { stdio: 'pipe' });
  console.log('network connected:', net, 'aliases:', aliases.join(','));
}
console.log('POST_CHECK: 等待 healthy 后验证 /api/health 版本烙印 + Tuwunel 登录探针（只输出状态码）：');
console.log('  docker inspect --format "{{.State.Health.Status}}" ' + NAME);
console.log('  curl -s http://127.0.0.1:<port>/api/health | grep version');
console.log('  在 console 容器内以 $MU_AGENTTEAMS_MATRIX_PASSWORD POST /_matrix/client/v3/login → 期待 200（不打印 token）');
