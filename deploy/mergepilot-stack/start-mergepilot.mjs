#!/usr/bin/env node
// start-mergepilot.mjs — MergePilot 一键启动入口（可测试核心，2026-10-22）。
//
// 设计：与 scripts/swap-console.cjs 同模式——deps.exec/deps.log/deps.sleep 可注入，
// 单元测试经 mock 全量覆盖校验逻辑；宿主执行=真实 spawnSync('docker', …)。
// start-mergepilot.ps1 为薄包装（powershell → node 本文件）。
//
// 用法：
//   node start-mergepilot.mjs --environment production [--iso-preview] [--dry-run]
//   node start-mergepilot.mjs --environment trial
//
// 组合规则（先于一切变更校验）：
//   * --environment production|trial 必填；
//   * trial + --iso-preview = 拒绝（iso 预览不启动真实试用服务）；
//   * production 必需：秘密三件（console.env/pg.env/github-app-key.pem）+
//     PG 数据卷 + keystore 卷 + 模型目录 + ctrl/manager 容器存在（start-only 前提）。
//     ——ctrl/manager 从零重建不在本工具能力内（CR env 人工限制，如实报错）。
//
// 序列（production）：
//   compose up 核心栈（pg healthcheck 门 → console）→ 健康门（version+完整 commit+
//   live+mu_schema_ready）→ 执行器双探针（workers+projects）→ bge 健康与模型身份。
// 序列（trial）：
//   compose up 试用真源（同 depends_on 门）→ console 健康门 → 完成。
//
// 秘密纪律：私钥读入进程 env 后经 compose 注入容器；日志/错误零回显；
// 脚本结束（成功/失败）均从进程 env 移除私钥。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));

// 本入口固化的发布身份（随每次发布更新；swap 换版后由 swap 流程同步）
export const RELEASE = Object.freeze({
  version: 'v0.2.0-beta.6-rc.35.6-rc7',
  commit: 'b7a0fcaf63ce3157a626c41918c8fdf60547ad74',
  image: 'ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.35.6-rc7',
});

export function parseArgv(argv) {
  const out = { environment: null, isoPreview: false, dryRun: false, positional: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--environment' || a === '-e') { out.environment = argv[++i] ?? null; continue; }
    if (a.startsWith('--environment=')) { out.environment = a.slice('--environment='.length); continue; }
    if (a === '--iso-preview') { out.isoPreview = true; continue; }
    if (a === '--dry-run') { out.dryRun = true; continue; }
    out.positional.push(a);
  }
  return out;
}

// 参数组合校验（纯函数；先于一切 docker 调用）
export function validateArgs(parsed) {
  const reasons = [];
  if (!['production', 'trial'].includes(parsed.environment)) {
    reasons.push('--environment 必须为 production|trial（收到: ' + String(parsed.environment) + '）');
  }
  if (parsed.environment === 'trial' && parsed.isoPreview) {
    reasons.push('trial + --iso-preview 组合不支持——iso 预览不启动真实试用服务；如需试用预览请用 production 环境');
  }
  return { ok: reasons.length === 0, reasons };
}

// 前置条件清单计算（纯函数；返回缺失项数组——空=通过）
export function precheckFailures({ secretFiles, modelManifest, volumes, containers },
  exec = (args) => spawnSync('docker', args, { encoding: 'utf8' })) {
  const missing = [];
  for (const f of secretFiles ?? []) if (!fs.existsSync(f)) missing.push('秘密文件缺失: ' + f);
  if (modelManifest && !fs.existsSync(modelManifest)) missing.push('模型清单缺失: ' + modelManifest);
  for (const v of volumes ?? []) {
    const r = exec(['volume', 'inspect', v]);
    if (r.status !== 0) missing.push('数据卷缺失: ' + v);
  }
  for (const c of containers ?? []) {
    const r = exec(['container', 'inspect', '--format', '{{.State.Running}}', c]);
    const running = String(r.stdout || '').trim() === 'true';
    if (!running) missing.push('前置容器不在运行（start-only 前提，本工具不从零重建）: ' + c);
  }
  return missing;
}

// 健康门校验（纯函数）：version 全等 + commit 全 40 位全等 + live + mu_ready
export function healthGateOk(health, { expectVersion, expectCommit }) {
  if (!health) return { ok: false, why: 'health 无响应' };
  if (health.ok !== true) return { ok: false, why: 'ok!=true' };
  if (expectVersion && health.version !== expectVersion) {
    return { ok: false, why: `version=${health.version} 期望 ${expectVersion}` };
  }
  const got = String(health.git_commit ?? '');
  if (expectCommit && got !== expectCommit) {
    return { ok: false, why: `git_commit=${got.slice(0, 12)} 期望 ${expectCommit.slice(0, 12)}（须完整 40 位全等）` };
  }
  if (health.data_mode !== 'live') return { ok: false, why: `data_mode=${health.data_mode}` };
  if (health.mu_schema_ready !== true) return { ok: false, why: 'mu_schema_ready=false' };
  return { ok: true, why: null };
}

// 执行器双探针结果判定（纯函数）
export function executorGateOk(probes) {
  const bad = (probes ?? []).filter((x) => x.status !== 200);
  return { ok: bad.length === 0, bad };
}

// ── 主入口 ──
export async function startMergepilot(argv, deps = {}) {
  const exec = deps.exec || ((args) => spawnSync('docker', args, { encoding: 'utf8' }));
  const log = deps.log || ((m) => process.stdout.write(m + '\n'));
  const sleep = deps.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const envGet = deps.envGet || ((k) => process.env[k] ?? null);
  const envSet = deps.envSet || ((k, v) => { process.env[k] = v; });
  const envDel = deps.envDel || ((k) => { delete process.env[k]; });

  const parsed = parseArgv(argv);
  const v = validateArgs(parsed);
  if (!v.ok) {
    log('参数组合校验失败——未执行任何变更：');
    for (const r of v.reasons) log('  - ' + r);
    return 2;
  }
  if (parsed.dryRun) { log('dry-run：校验通过（未执行变更）'); return 0; }

  const isProduction = parsed.environment === 'production';
  const envKeys = (inspect) => (inspect?.Config?.Env ?? []).map(envKeySafe);
  function envKeySafe(e) { const i = e.indexOf('='); return i === -1 ? e : e.slice(0, i); }

  if (!isProduction) {
    // ── 试用前置：秘密 .env + 服务容器在位（start-only 前提） ──
    const trialDir = deps.trialDir ?? 'D:/goai/r3work/accept-deploy-468/deploy/selfhost';
    const missing = precheckFailures({
      secretFiles: [path.join(trialDir, '.env')],
      containers: ['mp-trial-console', 'trial-bge'],
    });
    if (missing.length) {
      log('试用前置条件失败——未执行任何变更：');
      for (const m of missing) log('  - ' + m);
      return 3;
    }
    log('precheck: 通过（trial .env/服务容器在位）');
  }

  if (isProduction) {
    // ── 生产前置（缺失即报错，不宣称自动重建） ──
    const missing = precheckFailures({
      secretFiles: [
        path.join(deps.secretDir ?? defaultSecretDir(), 'beta-mp-console.env'),
        path.join(deps.secretDir ?? defaultSecretDir(), 'beta-mp-pg.env'),
        path.join(deps.secretDir ?? defaultSecretDir(), 'github-app-key.pem'),
      ],
      modelManifest: 'D:/goai/prod-data/rag-models/bge-m3/manifest.json',
      volumes: ['f2c57d69cc0e9b83a2440030b86abfb1f07ced641ce0c68ecdaa3c26efae1365', 'prod-keystore-vol'],
      containers: ['agentteams-beta-ctrl', 'agentteams-beta-manager'],
    });
    if (missing.length) {
      log('前置条件失败——未执行任何变更：');
      for (const m of missing) log('  - ' + m);
      if (missing.some((x) => x.includes('agentteams'))) {
        log('  注：ctrl/manager 为 start-only 前提——本工具不从零重建 agentteams（CR env 人工配置限制）。');
      }
      return 3;
    }
    log('precheck: 通过（秘密/模型/卷/ctrl-manager 前提）');
  }

  function defaultSecretDir() {
    return path.join(path.dirname(HERE), '..', 'secrets').replace(/\\/g, '/');
  }
  const secretDir = deps.secretDir ?? (isProduction
    ? path.join(path.dirname(HERE), '..', 'secrets').replace(/\\/g, '/')
    : null);

  // ── 私钥注入（多行 PEM；零回显；结束/失败路径均从进程 env 移除） ──
  if (isProduction) {
    const pemPath = path.join(secretDir, 'github-app-key.pem');
    envSet('MU_GITHUB_APP_PRIVATE_KEY', fs.readFileSync(pemPath, 'utf8'));
  }
  const cleanupEnv = () => { envDel('MU_GITHUB_APP_PRIVATE_KEY'); };
  try {
    if (isProduction) {
      if (!envGet('CONSOLE_IMAGE')) envSet('CONSOLE_IMAGE', RELEASE.image);
      if (!envGet('SECRET_DIR')) {
        envSet('SECRET_DIR', isProduction ? secretDir : HERE); // iso: iso.env 随仓在 deploy/mergepilot-stack/
      }
      const composeFile = path.join(HERE, 'docker-compose.yml');
      const up = exec(['compose', '-p', 'mergepilot', '-f', composeFile, 'up', '-d']);
      if (up.status !== 0) {
        log(`docker compose up 失败（exit=${up.status}）——不回显 stderr 详情；` +
            '可手动重跑诊断或按 DELIVERY-ACCEPTANCE.md 恢复配方回滚。');
        return await failWith(cleanupEnv, '核心栈 compose up 失败');
      }
      log('核心栈已拉起（pg healthcheck 门 → console）');
      // 健康门：48×5s
      let ok = false, lastWhy = '';
      for (let i = 0; i < 48; i++) {
        await sleep(5000);
        const probe = exec(['exec', 'beta-mp-console', 'node', '-e', HEALTH_SCRIPT]);
        let h = null;
        try { h = JSON.parse(String(probe.stdout || '')); } catch { }
        const g = healthGateOk(h, { expectVersion: deps.expectVersion ?? RELEASE.version, expectCommit: deps.expectCommit ?? RELEASE.commit });
        if (g.ok) { ok = true; log(`健康门通过（第 ${i + 1} 轮）`); break; }
        lastWhy = g.why;
        log(`健康检查 ${i + 1}/48: ${g.why}（等待）`);
      }
      if (!ok) return await failWith(cleanupEnv, '健康门 4 分钟未过：' + lastWhy);
      // 执行器双探针
      const probes = [];
      for (const p of ['workers', 'projects?limit=1']) {
        const r = exec(['exec', 'beta-mp-console', 'node', '-e',
          `fetch('http://at-auth-proxy:8091/api/v1/${p}',{headers:{authorization:'Bearer '+process.env.MU_AGENTTEAMS_TOKEN}}).then(r=>process.stdout.write(String(r.status))).catch(()=>process.stdout.write('0'))`]);
        probes.push({ probe: p, status: Number(String(r.stdout || '').trim()) });
      }
      const eg = executorGateOk(probes);
      if (!eg.ok) return await failWith(cleanupEnv, '执行器探针失败: ' + JSON.stringify(eg.bad));
      log('执行器双探针 200 ✓');
      // bge 健康与模型身份（模型清单在位 + 容器 healthy）
      const bgeHealth = exec(['inspect', '--format', '{{.State.Health.Status}}', 'beta-bge-sidecar']);
      if (String(bgeHealth.stdout || '').trim() !== 'healthy') {
        return await failWith(cleanupEnv, 'bge-sidecar 未 healthy');
      }
      log('bge-sidecar healthy ✓');
    }

    if (parsed.environment === 'trial') {
      const composeFile = path.join(HERE, 'docker-compose.trial.yml');
      const up = exec(['compose', '-p', 'mp-trial-rc355', '-f', composeFile, 'up', '-d']);
      if (up.status !== 0) {
        log(`试用基础设施 compose up 失败（exit=${up.status}）`);
        return await failWith(cleanupEnv, '试用基础设施 compose up 失败');
      }
      const st1 = exec(['start', 'trial-bge', 'mp-trial-console']);
      if (st1.status !== 0) {
        return await failWith(cleanupEnv, 'trial-bge / mp-trial-console start 失败（start-only 前提缺失？）');
      }
      // 健康门：18700 live + mu_schema_ready
      let ok = false, lastWhy = '';
      for (let i = 0; i < 48; i++) {
        await sleep(5000);
        const probe = exec(['exec', 'mp-trial-console', 'node', '-e', HEALTH_SCRIPT]);
        let h = null;
        try { h = JSON.parse(String(probe.stdout || '')); } catch { }
        const g = healthGateOk(h, { expectVersion: deps.expectVersion ?? RELEASE.version, expectCommit: deps.expectCommit ?? RELEASE.commit });
        if (g.ok) { ok = true; log(`试用健康门通过（第 ${i + 1} 轮）`); break; }
        lastWhy = g.why;
        log(`试用健康检查 ${i + 1}/48: ${g.why}（等待）`);
      }
      if (!ok) return await failWith(cleanupEnv, '试用健康门 4 分钟未过：' + lastWhy);
    }
  } finally {
    cleanupEnv();
  }
  log(`START OK (env=${parsed.environment})`);
  return 0;

  async function failWith(cleanup, why) {
    log('FAILED: ' + why);
    cleanup();
    return 1;
  }
}

const HEALTH_SCRIPT = "fetch('http://127.0.0.1:' + (process.env.CONSOLE_PORT || 4730) + '/api/health').then(r=>r.text()).then(t=>process.stdout.write(t)).catch(e=>{process.stdout.write('ERR');process.exit(1)})";

function defaultSecretDir() { return 'D:/goai/secrets'; }

// ── CLI ──
if (import.meta.url === 'file://' + process.argv[1] || process.argv[1]?.endsWith('start-mergepilot.mjs')) {
  const isMain = process.argv[1] && import.meta.url.endsWith(process.argv[1].split(/[\\/]/).pop());
  if (isMain || process.argv[1]?.endsWith('start-mergepilot.mjs')) {
    startMergepilot(process.argv.slice(2)).then((c) => process.exit(c)).catch((e) => {
      console.error('start-mergepilot: ' + (e && e.message ? e.message : e));
      process.exit(1);
    });
  }
}
