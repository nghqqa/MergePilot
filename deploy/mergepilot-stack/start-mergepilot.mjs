#!/usr/bin/env node
// start-mergepilot.mjs — MergePilot 一键启动入口 v3（2026-10-23 行为闭环版）。
//
// v3 修正（#512 行为闭环）：
//   * 隔离模式独立分派：--environment production --iso-preview 在任何生产前置检查
//     之前进入 iso 流程——零生产秘密/容器/卷触达，compose = 基座+iso 覆盖双 -f，
//     项目名 mergepilot-iso，console=127.0.0.1:14730；
//   * compose 显式供参：${CONSOLE_IMAGE}=RELEASE.image、${SECRET_DIR}=脚本内
//     iso-secrets 假目录（iso）/受控秘密目录（生产）、${MU_GITHUB_APP_PRIVATE_KEY}
//     显式空值（iso）——不依赖调用者残留变量；
//   * 进程 env 带快照恢复：成功与失败路径均还原调用前的值（残留值回填、未设值清除）。
// v2 修正（#511）保留：
//   * 停止/缺失区分：start-only 容器「存在但停止」→ docker start；「不存在」→ 明确报错
//     （start-only 前提缺失，本工具不从零重建）；
//   * 配置显式化（env → CLI → 宿主常量，不依赖 CWD）；版本烙印不随 env 走；
//   * 私钥多行 PEM 零回显；每条关键 docker 命令检查退出码。
//
// 已知边界（如实）：ctrl/manager 从零重建、worker CR env 人工配置 = 前提，不在本工具能力内。
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { spawnSync } from 'node:child_process';

// compose 真源与本脚本同目录（不依赖调用方 CWD）
const HERE = path.dirname(fileURLToPath(import.meta.url));

// 本入口固化的发布身份（随每次发布更新；swap 换版后由 swap 流程同步）
export const RELEASE = Object.freeze({
  version: 'v0.2.0-beta.6-rc.35.6-rc7',
  commit: 'b7a0fcaf63ce3157a626c41918c8fdf60547ad74',
  image: 'ghcr.io/nghqqa/mergepilot-console:v0.2.0-beta.6-rc.35.6-rc7',
});

// 宿主常量（Windows 宿主；生产/试用数据与秘密的稳定位置）
export const HOST = Object.freeze({
  secretDir: 'D:/goai/secrets',
  trialDir: 'D:/goai/r3work/accept-deploy-468/deploy/selfhost',
  modelManifest: 'D:/goai/prod-data/rag-models/bge-m3/manifest.json',
  pgDataVolume: 'f2c57d69cc0e9b83a2440030b86abfb1f07ced641ce0c68ecdaa3c26efae1365',
  keystoreVolume: 'prod-keystore-vol',
});

export function parseArgv(argv) {
  const out = { environment: null, isoPreview: false, dryRun: false, secretDir: null, positional: [] };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--environment' || a === '-e') { out.environment = argv[++i] ?? null; continue; }
    if (a.startsWith('--environment=')) { out.environment = a.slice('--environment='.length); continue; }
    if (a === '--iso-preview') { out.isoPreview = true; continue; }
    if (a === '--dry-run') { out.dryRun = true; continue; }
    if (a === '--secret-dir') { out.secretDir = argv[++i] ?? null; continue; }
    if (a.startsWith('--secret-dir=')) { out.secretDir = a.slice('--secret-dir='.length); continue; }
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

// 前置条件检查（exec 可注入——单测零 Docker）。
// 返回 { missing, stopped }：missing=明确报错项（不存在）；stopped=存在但停止（由流程拉起）。
export function precheckFailures({ secretFiles = [], modelManifest = null, containers = [], volumes = [] } = {}, exec) {
  const missing = [];
  const stopped = [];
  for (const f of secretFiles ?? []) {
    if (!fs.existsSync(f)) missing.push('秘密文件缺失: ' + f);
  }
  if (modelManifest && !fs.existsSync(modelManifest)) missing.push('模型清单缺失: ' + modelManifest);
  for (const c of containers ?? []) {
    const r = exec(['container', 'inspect', '--format', '{{.State.Status}}|{{.State.Running}}', c]);
    const [status, running] = String(r.stdout || '').trim().split('|');
    if (r.status !== 0 || status === '') {
      missing.push('start-only 前提缺失（容器不存在，本工具不从零重建）: ' + c); continue;
    }
    if (running !== 'true') stopped.push(c);
  }
  for (const v of volumes ?? []) {
    const r = exec(['volume', 'inspect', v]);
    if (r.status !== 0) missing.push('数据卷缺失: ' + v);
  }
  return { missing, stopped };
}

// 健康门（纯函数）：version 全等 + commit 全 40 位全等 + live + mu_ready
export function healthGateOk(health, { expectVersion, expectCommit } = {}) {
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

  // 进程 env 变更带快照：成功/失败路径均恢复调用前的值（残留值回填、未设值清除）
  const priorEnv = new Map();
  const setTracked = (k, v) => { if (!priorEnv.has(k)) priorEnv.set(k, envGet(k)); envSet(k, v); };
  const cleanupEnv = () => {
    for (const [k, prior] of priorEnv) { if (prior === null || prior === undefined) envDel(k); else envSet(k, prior); }
  };

  const parsed = parseArgv(argv);
  const v = validateArgs(parsed);
  if (!v.ok) {
    log('参数组合校验失败——未执行任何变更：');
    for (const r of v.reasons) log('  - ' + r);
    return 2;
  }
  if (parsed.dryRun) { log('dry-run：校验通过（未执行变更）'); return 0; }

  const isProduction = parsed.environment === 'production';
  const isIso = parsed.isoPreview === true; // validateArgs 已保证 trial+iso 不可能到达这里
  const secretDir = parsed.secretDir ?? deps.secretDir ?? HOST.secretDir;

  // ── 隔离预览：独立分派，先于一切生产前置（零生产秘密/容器/卷触达）──
  if (isProduction && isIso) {
    const isoFiles = [
      path.join(HERE, 'iso.env'),
      path.join(HERE, 'docker-compose.yml'),
      path.join(HERE, 'docker-compose.iso.yml'),
      path.join(HERE, 'iso-secrets', 'beta-mp-pg.env'),
      path.join(HERE, 'iso-secrets', 'beta-mp-console.env'),
    ];
    const missingFiles = isoFiles.filter((f) => !fs.existsSync(f));
    if (missingFiles.length) {
      log('隔离配置预检失败——未执行任何变更：');
      for (const m of missingFiles) log('  - 缺失: ' + m);
      return 3;
    }
    // 显式提供 compose 所需镜像与环境（不依赖调用者残留变量）：
    //  * 基座 compose 的 env_file 路径含 ${SECRET_DIR}——解析期读取 iso-secrets 假文件，
    //    合并配置中被 iso 覆盖丢弃（pg env_file 清空/console 换 iso.env）——生产秘密不入隔离；
    //  * ${CONSOLE_IMAGE} 显式取 RELEASE.image；${MU_GITHUB_APP_PRIVATE_KEY} 显式空值。
    setTracked('SECRET_DIR', path.join(HERE, 'iso-secrets'));
    setTracked('CONSOLE_IMAGE', RELEASE.image);
    setTracked('MU_GITHUB_APP_PRIVATE_KEY', '');
    try {
      const up = exec(['compose', '-p', 'mergepilot-iso', '-f', path.join(HERE, 'docker-compose.yml'),
        '-f', path.join(HERE, 'docker-compose.iso.yml'), 'up', '-d']);
      if (up.status !== 0) { log(`FAILED: 隔离栈 compose up（exit=${up.status}）`); return await failAll('隔离栈 compose up 失败'); }
      log('隔离栈已拉起（项目 mergepilot-iso，console=127.0.0.1:14730）');
      let ok = false, lastWhy = '';
      for (let i = 0; i < 48; i++) {
        await sleep(5000);
        const probe = exec(['exec', 'mergepilot-iso-console', 'node', '-e', HEALTH_SCRIPT]);
        let h = null;
        try { h = JSON.parse(String(probe.stdout || '')); } catch { }
        const g = healthGateOk(h, { expectVersion: RELEASE.version, expectCommit: RELEASE.commit });
        if (g.ok) { ok = true; log(`隔离健康门通过（第 ${i + 1} 轮）`); break; }
        lastWhy = g.why;
        log(`隔离健康检查 ${i + 1}/48: ${g.why}（等待）`);
      }
      if (!ok) { log('FAILED: 隔离健康门 4 分钟未过：' + lastWhy); return await failAll('隔离健康门 4 分钟未过'); }
    } finally {
      cleanupEnv();
    }
    log('START OK (env=production+iso-preview)');
    return 0;
  }

  if (isProduction) {
    // ── 生产前置：秘密/模型/卷存在 + ctrl/manager 存在（start-only 前提）──
    const { missing } = precheckFailures({ secretFiles: [
      path.join(secretDir, 'beta-mp-console.env'),
      path.join(secretDir, 'beta-mp-pg.env'),
      path.join(secretDir, 'github-app-key.pem'),
    ], modelManifest: HOST.modelManifest,
      volumes: [HOST.pgDataVolume, HOST.keystoreVolume],
      containers: ['agentteams-beta-ctrl', 'agentteams-beta-manager'],
    }, exec);
    if (missing.length) {
      log('生产前置条件失败——未执行任何变更：');
      for (const m of missing) log('  - ' + m);
      if (missing.some((x) => x.includes('agentteams'))) {
        log('  注：ctrl/manager 为 start-only 前提——本工具不从零重建 agentteams（CR env 人工配置限制）。');
      }
      return 3;
    }
    log('precheck: 通过（生产秘密/模型/卷/ctrl-manager 前提）');
  }

  // ── 私钥注入（仅生产；多行 PEM 读入进程 env，零回显；finally 恢复调用前环境）──
  if (isProduction) {
    const pemPath = path.join(secretDir, 'github-app-key.pem');
    setTracked('SECRET_DIR', secretDir);
    setTracked('CONSOLE_IMAGE', RELEASE.image); // compose ${CONSOLE_IMAGE} 显式取发布身份，不依赖调用者残留
    setTracked('MU_GITHUB_APP_PRIVATE_KEY', fs.readFileSync(pemPath, 'utf8'));
  }

  try {
    if (isProduction) {
      // ── agentteams ctrl/manager：存在但停止 → start 并等 Running；运行中 → 幂等跳过 ──
      for (const c of ['agentteams-beta-ctrl', 'agentteams-beta-manager']) {
        const st = exec(['container', 'inspect', '--format', '{{.State.Running}}', c]);
        if (String(st.stdout || '').trim() !== 'true') {
          const s = exec(['start', c]);
          if (s.status !== 0) { log(`FAILED: ${c} start 失败（exit=${s.status}）`); return await failAll('agentteams start 失败'); }
          for (let w = 0; w < 12; w++) {
            const chk = exec(['container', 'inspect', '--format', '{{.State.Running}}', c]);
            if (String(chk.stdout || '').trim() === 'true') break;
            await sleep(2000);
          }
          log(`started: ${c}`);
        }
      }
      // ── 核心栈 compose up（pg healthcheck 门 → console；atnet .5 与固定配置随 compose）──
      const composeFile = path.join(HERE, 'docker-compose.yml');
      const up = exec(['compose', '-p', 'mergepilot', '-f', composeFile, 'up', '-d']);
      if (up.status !== 0) {
        log(`核心栈 compose up 失败（exit=${up.status}）——不回显 stderr 详情；可手动重跑或按 DELIVERY-ACCEPTANCE.md 恢复配方回滚。`);
        return await failAll('核心栈 compose up 失败');
      }
      log('核心栈已拉起（pg healthcheck 门 → console）');
      // ── 健康门：48×5s ──
      let ok = false, lastWhy = '';
      for (let i = 0; i < 48; i++) {
        await sleep(5000);
        const probe = exec(['exec', 'beta-mp-console', 'node', '-e', HEALTH_SCRIPT]);
        let h = null;
        try { h = JSON.parse(String(probe.stdout || '')); } catch { }
        const g = healthGateOk(h, { expectVersion: RELEASE.version, expectCommit: RELEASE.commit });
        if (g.ok) { ok = true; log(`健康门通过（第 ${i + 1} 轮）`); break; }
        lastWhy = g.why;
        log(`健康检查 ${i + 1}/48: ${g.why}（等待）`);
      }
      if (!ok) { log('FAILED: 健康门 4 分钟未过：' + lastWhy); return await failAll('健康门 4 分钟未过：' + lastWhy); }
      // ── 执行器双探针（workers+projects）──
      const probes = [];
      for (const p of ['workers', 'projects?limit=1']) {
        const r = exec(['exec', 'beta-mp-console', 'node', '-e',
          `fetch('http://at-auth-proxy:8091/api/v1/${p}',{headers:{authorization:'Bearer '+process.env.MU_AGENTTEAMS_TOKEN}}).then(r=>process.stdout.write(String(r.status))).catch(()=>process.stdout.write('0'))`]);
        probes.push({ probe: p, status: Number(String(r.stdout || '').trim()) });
      }
      const eg = executorGateOk(probes);
      if (!eg.ok) { log('FAILED: 执行器探针失败: ' + JSON.stringify(eg.bad)); return await failAll('执行器探针失败'); }
      log('执行器双探针 200 ✓');
      // ── bge 健康与模型身份 ──
      const bgeHealth = exec(['inspect', '--format', '{{.State.Health.Status}}', 'beta-bge-sidecar']);
      if (String(bgeHealth.stdout || '').trim() !== 'healthy') {
        log('FAILED: bge-sidecar 未 healthy'); return await failAll('bge-sidecar 未 healthy');
      }
      log('bge-sidecar healthy ✓');
    }

    if (parsed.environment === 'trial') {
      // ── 试用基础设施 compose up（postgres 数据卷=外部既有卷；webhook-ingress）──
      const composeFile = path.join(HERE, 'docker-compose.trial.yml');
      const pw = parseTrialPassword(path.join(deps.trialDir ?? HOST.trialDir, '.env'));
      setTracked('POSTGRES_PASSWORD', pw);
      setTracked('WEBHOOK_INGRESS_PORT', '18590'); // compose ${WEBHOOK_INGRESS_PORT:-18590} 显式定值，不依赖调用者残留
      const up = exec(['compose', '-p', 'mp-trial-rc355', '-f', composeFile, 'up', '-d']);
      if (up.status !== 0) { log(`FAILED: 试用基础设施 compose up（exit=${up.status}）`); return await failAll('试用基础设施 compose up 失败'); }
      // ── 服务容器：存在但停止 → start；运行中 → 幂等跳过 ──
      for (const c of ['trial-bge', 'mp-trial-console']) {
        const st = exec(['container', 'inspect', '--format', '{{.State.Running}}', c]);
        if (String(st.stdout || '').trim() !== 'true') {
          const s = exec(['start', c]);
          if (s.status !== 0) { log(`FAILED: ${c} start 失败（exit=${s.status}）`); return await failAll(`${c} start 失败`); }
          log(`started: ${c}`);
        }
      }
      // ── 健康门：18700 live + mu_schema_ready ──
      let ok = false, lastWhy = '';
      for (let i = 0; i < 48; i++) {
        await sleep(5000);
        const probe = exec(['exec', 'mp-trial-console', 'node', '-e', HEALTH_SCRIPT]);
        let h = null;
        try { h = JSON.parse(String(probe.stdout || '')); } catch { }
        const g = healthGateOk(h, { expectVersion: RELEASE.version, expectCommit: RELEASE.commit });
        if (g.ok) { ok = true; log(`试用健康门通过（第 ${i + 1} 轮）`); break; }
        lastWhy = g.why;
        log(`试用健康检查 ${i + 1}/48: ${g.why}（等待）`);
      }
      if (!ok) { log('FAILED: 试用健康门 4 分钟未过：' + lastWhy); return await failAll('试用健康门 4 分钟未过'); }
    }
  } finally {
    cleanupEnv(); // 成功/失败路径均清理私钥进程 env（零残留）
  }
  log(`START OK (env=${parsed.environment})`);
  return 0;

  function failAll(why) {
    log('FAILED: ' + why);
    cleanupEnv();
    return 1;
  }
}

const HEALTH_SCRIPT = "fetch('http://127.0.0.1:' + (process.env.CONSOLE_PORT || 4730) + '/api/health').then(r=>r.text()).then(t=>process.stdout.write(t)).catch(e=>{process.stdout.write('ERR');process.exit(1)})";

// 试用 .env 的 POSTGRES_PASSWORD 解析（零回显）
function parseTrialPassword(file) {
  const m = fs.readFileSync(file, 'utf8').match(/^POSTGRES_PASSWORD=(.+)$/m);
  return m ? m[1] : '';
}

// ── CLI ──
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  startMergepilot(process.argv.slice(2)).then((c) => process.exit(c)).catch((e) => {
    console.error('start-mergepilot: ' + (e && e.message ? e.message : e));
    process.exit(1);
  });
}
