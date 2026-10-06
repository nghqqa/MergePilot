#!/usr/bin/env node
// MergePilot 自托管 preflight — 一键只读检查（零依赖，node >= 20）。
// 用法：node preflight.mjs [--live] [--env .env]
//   --live  栈已启动时额外探测 health/schema/cchain attest/bge/queue（HTTP+docker exec）
// 纪律：只读；不输出任何 secret/token/cookie/DSN 密码（仅键名与掩码值）。
// 退出码：0=全部关键项通过；1=任一关键项失败（fail-closed）；2=用法/环境错误。
// 作为模块：`import { probeAttest } from './preflight.mjs'` 可复用 attest 端点探测
//（test-attest-probe.mjs 即基于此做八场景隔离自测）。
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// ── attest 端点探测（可导出复用；与 console 运行时 fetchProviderAttestation 同口径）──
// 返回 { outcome, detail }；outcome ∈ pass（200+形状通过[+与本地源文件一致]）/
//   warn（宿主侧探测不到 compose 内网名）/ fail（不可达、非 200、内容校验失败）。
export async function probeAttest(url, opts = {}) {
  const file = opts.file || '';
  let body = '';
  try {
    const res = await (opts.fetchImpl || fetch)(url, {
      signal: AbortSignal.timeout(opts.timeoutMs || 5000),
      headers: { accept: 'application/json' },
    });
    if (res.status !== 200) {
      return { outcome: 'fail', detail: `HTTP ${res.status}（非 200：服务未启动或 attestation.json 缺失/路径错）` };
    }
    body = await res.text();
  } catch (e) {
    const msg = String(e?.message || e);
    let host = url;
    try { host = new URL(url).hostname; } catch {}
    if (host === '127.0.0.1' || host === 'localhost' || host === '::1') {
      return { outcome: 'fail', detail: `不可达：${msg}` };
    }
    return { outcome: 'warn', detail: `宿主侧不可达（${msg}）——URL 若为 compose 内网名（如 http://attest/...），宿主探测不到属正常；栈内可达性以 /api/cchain/status 为准` };
  }
  let j;
  try { j = JSON.parse(body); } catch { return { outcome: 'fail', detail: '内容校验失败：响应非 JSON' }; }
  const problems = [];
  for (const k of ['provider', 'model', 'attestation']) {
    if (!j || !j[k]) problems.push('missing ' + k);
  }
  if (j && (Array.isArray(j.attestation) ? j.attestation.length === 0 : typeof j.attestation !== 'object' || j.attestation === null)) {
    problems.push('attestation 为空');
  }
  if (problems.length) return { outcome: 'fail', detail: '内容校验失败：' + problems.join(', ') };
  if (file) {
    if (!fs.existsSync(file)) return { outcome: 'fail', detail: `内容校验失败：本地源文件缺失（${file}）` };
    let local = '';
    try { local = fs.readFileSync(file, 'utf8'); } catch (e) {
      return { outcome: 'fail', detail: `内容校验失败：本地源文件不可读（${String(e?.message || e)}）` };
    }
    if (local.trim() !== String(body).trim()) return { outcome: 'fail', detail: '内容校验失败：端点响应与本地源文件不一致' };
  }
  return { outcome: 'pass', detail: 'HTTP 200 + 形状校验通过' + (file ? ' + 与本地源文件一致' : '') };
}

let pass = 0, fail = 0, warn = 0;
const ok = (n, c, d) => { c ? pass++ : fail++; console.log((c ? '  PASS  ' : '  FAIL  ') + n + (d ? '  ' + d : '')); };
const warnk = (n, d) => { warn++; console.log('  WARN  ' + n + (d ? '  ' + d : '')); };
const mask = (v) => { if (v == null) return '(空)'; const s = String(v); return s.length <= 6 ? '***' : s.slice(0, 4) + '…***(' + s.length + ')'; };
const run = (cmd, opts = {}) => spawnSync(cmd[0], cmd.slice(1), { encoding: 'utf8', ...opts });

async function main() {
  const args = process.argv.slice(2);
  const LIVE = args.includes('--live');
  const envIdx = args.indexOf('--env');
  const ENV_FILE = envIdx >= 0 ? args[envIdx + 1] : path.join(process.cwd(), '.env');
  const COMPOSE_FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'docker-compose.yml');

  console.log('== MergePilot preflight ==\n');

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

  // ── 2b. cchain 三键（可选）与 attest URL 静态检查 ──
  {
    const cc3 = ['MERGEPILOT_MODEL_CACHE_DIR', 'MERGEPILOT_PROVIDER_ATTEST_URL', 'MERGEPILOT_RUN_BINDING_KEYSTORE'];
    const have = cc3.filter((k) => env[k]).length;
    if (have === 3) ok('cchain 三键已配置', true);
    else warnk(`cchain 三键 ${have}/3（不全则 cchain=BLOCKED，fail-closed 如实呈现；引导见 README「provider attestation」节）`);
    if (env.MERGEPILOT_PROVIDER_ATTEST_URL && !/^https?:\/\//.test(env.MERGEPILOT_PROVIDER_ATTEST_URL)) {
      ok('MERGEPILOT_PROVIDER_ATTEST_URL 须以 http(s):// 开头', false, mask(env.MERGEPILOT_PROVIDER_ATTEST_URL));
    }
  }

  // ── 3. compose 模板可解析 ──
  const cc = run(['docker', 'compose', '-f', COMPOSE_FILE, '--env-file', ENV_FILE, 'config', '--quiet']);
  ok('compose 模板解析通过', cc.status === 0, cc.stderr && cc.stderr.split('\n')[0]);

  // ── 4. 镜像 digest / version ──
  const ci = run(['docker', 'images', '--format', '{{.ID}} {{.Repository}}:{{.Tag}}']);
  const hasConsoleLocal = ci.stdout.split('\n').some((l) => /mergepilot-console/.test(l));
  ok('console 镜像已存在于本地镜像列表', hasConsoleLocal, hasConsoleLocal ? ci.stdout.split('\n').find((l) => /mergepilot-console/.test(l)) : '先 docker pull/load（离线 tar 或 GHCR digest）');
  {
    const ver = run(['docker', 'run', '--rm', '--entrypoint', 'sh',
      (ci.stdout.split('\n').find((l) => /mergepilot-console:v/.test(l)) || 'x').split(' ').pop(),
      '-c', 'echo $MERGEPILOT_VERSION']);
    if (ver.status === 0) ok('镜像内置 MERGEPILOT_VERSION=' + (ver.stdout || '(空)').trim(), (ver.stdout || '').trim().length > 0, '版本随镜像构建注入，不做硬编码断言');
    else warnk('镜像 version 检查跳过（本地无 tag 形式镜像，属正常——官方分发按 digest 拉取）');
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
    ok('health ok（version=' + (h?.version || '无') + '）', !!h && h.ok === true, '版本随镜像注入，显示供核对');
    ok('schema（mu_schema_ready）', !!h?.mu_schema_ready);
    const runs = await fetch(base + '/api/mu/runs', { headers: { cookie: 'x=p' } }).then((r) => r.status).catch(() => 0);
    ok('MU API 会话门（未认证 401/403）', runs === 401 || runs === 403, 'status=' + runs);
    const pg = run(['docker', 'exec', 'mergepilot-postgres-1', 'psql', '-U', 'postgres', '-d', 'mu', '-tAc',
      "SELECT 'schema=' || COALESCE(MAX(version),0) || ' 非终态run=' || (SELECT COUNT(*) FROM mu.review_run WHERE status NOT IN ('COMPLETED','FAILED','CANCELLED','BLOCKED','STALE','SUPERSEDED')) || ' 非终态job=' || (SELECT COUNT(*) FROM mu.job WHERE state IN ('queued','running','claimed','pending','processing')) || ' pa_inv=' || (SELECT COUNT(*) FROM mu.invitation WHERE role='platform_admin') FROM mu.schema_migrations"]).stdout.trim();
    const sm = pg.match(/schema=(\d+)/);
    ok('schema v' + (sm ? sm[1] : '?') + '（>=23）+ 非终态=0 + pa_inv=0',
      !!sm && Number(sm[1]) >= 23 && / 非终态run=0 /.test(pg) && / 非终态job=0 /.test(pg) && / pa_inv=0/.test(pg), pg);

    // ── 8. provider attestation 端点探测（cchain 三键之二）──
    if (env.MERGEPILOT_PROVIDER_ATTEST_URL) {
      const r = await probeAttest(env.MERGEPILOT_PROVIDER_ATTEST_URL, { file: env.MERGEPILOT_PROVIDER_ATTEST_FILE });
      if (r.outcome === 'pass') ok('attest 端点：' + r.detail, true);
      else if (r.outcome === 'warn') warnk('attest 端点：' + r.detail);
      else ok('attest 端点：' + r.detail, false);
    } else {
      warnk('MERGEPILOT_PROVIDER_ATTEST_URL 未设置 → provider_attestation=NOT_CONFIGURED → cchain=BLOCKED（可选组件，不影响审查主链）');
    }

    console.log('  HINT  cchain=BLOCKED 属如实呈现：四步引导见 README cchain 节（模型+manifest / fxv.audit_events 建表 / keystore 种子密钥 uid 1000 / provider attestation 端点）');
    console.log('  HINT  webhook：GitHub App 安装/配置后，用 App 高级页 Redeliver 一条历史投递做端到端验证');
  } else {
    console.log('  HINT  加 --live 可在栈启动后探测 health/schema/attest 端点/queue（当前为静态检查）');
  }

  console.log(`\n${pass} pass, ${fail} fail, ${warn} warn`);
  process.exit(fail ? 1 : 0);
}

const __argv1 = process.argv[1] ? path.resolve(process.argv[1]) : '';
if (__argv1 && import.meta.url === pathToFileURL(__argv1).href) {
  await main();
}
