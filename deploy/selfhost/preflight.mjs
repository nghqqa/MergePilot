#!/usr/bin/env node
// MergePilot rc.14 自托管 preflight — 一键只读检查（零依赖，node >= 20）。
// 用法：node preflight.mjs [--live] [--env .env]
//   --live  栈已启动时额外探测 health/schema/cchain/bge/queue（HTTP+docker exec）
// 纪律：只读；不输出任何 secret/token/cookie/DSN 密码（仅键名与掩码值）。
// 退出码：0=全部关键项通过；1=任一关键项失败（fail-closed）；2=用法/环境错误。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const LIVE = args.includes('--live');
const envIdx = args.indexOf('--env');
const ENV_FILE = envIdx >= 0 ? args[envIdx + 1] : path.join(process.cwd(), '.env');
const COMPOSE_FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'docker-compose.yml');

let pass = 0, fail = 0, warn = 0;
const ok = (n, c, d) => { c ? pass++ : fail++; console.log((c ? '  PASS  ' : '  FAIL  ') + n + (d ? '  ' + d : '')); };
const warnk = (n, d) => { warn++; console.log('  WARN  ' + n + (d ? '  ' + d : '')); };
const mask = (v) => { if (v == null) return '(空)'; const s = String(v); return s.length <= 6 ? '***' : s.slice(0, 4) + '…***(' + s.length + ')'; };
const run = (cmd, opts = {}) => spawnSync(cmd[0], cmd.slice(1), { encoding: 'utf8', ...opts });

console.log('== MergePilot rc.14 preflight ==\n');

// ── 1. Docker / Compose 版本 ──
const dv = run(['docker', 'version', '--format', '{{.Server.Version}}']);
ok('Docker 可用（server v' + (dv.stdout || '').trim() + '）', dv.status === 0, dv.stderr && dv.stderr.slice(0, 80));
const cv = run(['docker', 'compose', 'version', '--short']);
if (cv.status === 0) ok('Docker Compose v2（v' + cv.stdout.trim() + '）', true);
else warnk('docker compose v2 不可用（仅影响 up 命令，preflight 其余项继续）');

// ── 2. .env 必填项与权限 ──
const REQUIRED = [
  'POSTGRES_PASSWORD', 'CONSOLE_SESSION_SECRET',
  'MU_GITHUB_APP_ID', 'MU_GITHUB_APP_PRIVATE_KEY', 'MU_GITHUB_APP_SLUG', 'MU_GITHUB_WEBHOOK_SECRET',
  'MU_GITHUB_OAUTH_CLIENT_ID', 'MU_GITHUB_OAUTH_CLIENT_SECRET',
  'MU_GITHUB_OAUTH_CALLBACK_URL', 'MU_GITHUB_APP_INSTALL_CALLBACK_URL',
  'MU_LLM_PROVIDER', 'MU_LLM_BASE_URL', 'MU_LLM_MODEL', 'MU_LLM_API_KEY',
];
if (!fs.existsSync(ENV_FILE)) {
  fail++; console.log('  FAIL  .env 不存在：cp .env.example .env 并填写（--env 可指定路径）');
  process.exit(1);
}
const envText = fs.readFileSync(ENV_FILE, 'utf8');
const env = {};
for (const line of envText.split(/\r?\n/)) {
  const m = line.match(/^([A-Z][A-Z0-9_]*)=(.*)$/);
  if (m) env[m[1]] = m[2].trim();
}
const missing = REQUIRED.filter((k) => !env[k]);
ok('必填环境变量齐全（' + (REQUIRED.length - missing.length) + '/' + REQUIRED.length + '）', missing.length === 0, missing.length ? '缺失: ' + missing.join(',') : undefined);
for (const k of ['CONSOLE_SESSION_SECRET', 'MU_GITHUB_WEBHOOK_SECRET']) {
  if (env[k] && env[k].length < 32) warnk(k + ' 强度不足（<32 字符），建议 openssl rand -base64 32');
}
const st = fs.statSync(ENV_FILE);
ok('.env 权限（建议 600，当前 ' + (st.mode & 0o777).toString(8) + '）', (st.mode & 0o077) === 0, 'Windows/共享盘下可忽略此项');
const leaked = Object.entries(env).filter(([k, v]) => /sk-[A-Za-z0-9]{20,}/.test(v) && k !== 'MU_LLM_API_KEY');
ok('.env 无疑似误粘的第三方凭据格式', leaked.length === 0, leaked.map(([k]) => k));

// ── 3. compose 模板可解析 ──
const cc = run(['docker', 'compose', '-f', COMPOSE_FILE, '--env-file', ENV_FILE, 'config', '--quiet']);
ok('compose 模板解析通过', cc.status === 0, cc.stderr && cc.stderr.split('\n')[0]);

// ── 4. 镜像 digest / version ──
const imgRef = (envText.match(/^#\s*console digest:\s*(\S+)$/m) || [])[1];
const ci = run(['docker', 'images', '--format', '{{.ID}} {{.Repository}}:{{.Tag}}']);
const hasConsoleLocal = ci.stdout.split('\n').some((l) => /mergepilot-console/.test(l));
ok('console 镜像已存在于本地镜像列表', hasConsoleLocal, hasConsoleLocal ? ci.stdout.split('\n').find((l) => /mergepilot-console/.test(l)) : '先 docker pull/load（GHCR 推送生效前请按 README 源码构建或用离线 tar）');
{
  const ver = run(['docker', 'run', '--rm', '--entrypoint', 'sh',
    (ci.stdout.split('\n').find((l) => /mergepilot-console:v/.test(l)) || 'x').split(' ').pop(),
    '-c', 'echo $MERGEPILOT_VERSION']);
  if (ver.status === 0) ok('镜像内置 MERGEPILOT_VERSION=' + ver.stdout.trim(), /rc\.14/.test(ver.stdout));
  else warnk('镜像 version 检查跳过（镜像未就绪）');
}

// ── 5. 端口冲突 ──
{
  const portA = env.CONSOLE_PORT || '48500';
  const portB = env.WEBHOOK_INGRESS_PORT || '48590';
  const ns = run(['netstat', '-ano']);
  const clash = [portA, portB].filter((p) => ns.stdout.split('\n').some((l) => l.includes(':' + p + ' ') && /LISTEN/i.test(l)));
  // 已由本 compose 占用的端口不算冲突（复跑场景）
  ok('宿主端口无占用冲突（' + portA + ' / ' + portB + '）', clash.length === 0, clash.length ? '被占用: ' + clash.join(',') + '（若为上一轮本栈可忽略）' : undefined);
}

// ── 6. volume/网络/目录 ──
{
  const vols = run(['docker', 'volume', 'ls', '--format', '{{.Name}}']);
  const want = ['mergepilot_pgdata', 'mergepilot_evidence', 'mergepilot_model-cache', 'mergepilot_keystore'];
  const have = want.filter((v) => vols.stdout.includes(v));
  if (have.length) ok('既有数据卷识别（' + have.length + '/' + want.length + '）', true);
  else warnk('尚无数据卷（首次部署属正常）');
}

// ── 7. live 项（--live 时）──
if (LIVE) {
  const port = env.CONSOLE_PORT || '48500';
  const base = 'http://127.0.0.1:' + port;
  const h = await fetch(base + '/api/health').then((r) => r.json()).catch(() => null);
  ok('health ok + version=rc.14', !!h && h.ok === true && /rc\.14/.test(h.version || ''), h ? 'version=' + h.version : '无响应');
  ok('schema（mu_schema_ready）', !!h?.mu_schema_ready);
  const runs = await fetch(base + '/api/mu/runs', { headers: { cookie: 'x=p' } }).then((r) => r.status).catch(() => 0);
  ok('MU API 会话门（未认证 401/403）', runs === 401 || runs === 403, 'status=' + runs);
  const pg = run(['docker', 'exec', 'mergepilot-postgres-1', 'psql', '-U', 'postgres', '-d', 'mu', '-tAc',
    "SELECT 'schema=' || COALESCE(MAX(version),0) || ' 非终态run=' || (SELECT COUNT(*) FROM mu.review_run WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED','BLOCKED','STALE','SUPERSEDED')) || ' 非终态job=' || (SELECT COUNT(*) FROM mu.job WHERE state IN ('queued','running','claimed','pending','processing')) || ' pa_inv=' || (SELECT COUNT(*) FROM mu.invitation WHERE role='platform_admin') FROM mu.schema_migrations"]).stdout.trim();
  ok('schema v23 + 非终态=0 + pa_inv=0', /schema=23 .* 非终态run=0 .* 非终态job=0 .* pa_inv=0/.test(pg), pg);
  console.log('  HINT  cchain=BLOCKED 属如实呈现：补齐 attestation 端点与 keystore 轮换后转 READY（README cchain 节）');
  console.log('  HINT  webhook：GitHub App 安装/配置后，用 App 高级页 Redeliver 一条历史投递做端到端验证');
} else {
  console.log('  HINT  加 --live 可在栈启动后探测 health/schema/queue（当前为静态检查）');
}

console.log(`\n${pass} pass, ${fail} fail, ${warn} warn`);
process.exit(fail ? 1 : 0);
