#!/usr/bin/env node
// scripts/swap-console.cjs — 容器滚动替换（swap）工具：从 docker inspect JSON 完整继承运行时配置。
//
// 背景（rc.11 生产滚动实录，本文件是 r3work 运维侧 swap2.cjs 的仓库规范化版本）：
//   1. swap2.cjs 硬编码 `--restart no` → 每次滚动冲掉已批准的 unless-stopped
//      （rc.11 生产滚动实际发生：生产容器 policy 回归 no，daemon 重启不自愈）；
//   2. 挂载靠调用方手工 `--mount=` 传参 → 首次滚动漏传 rag-models/rag-corpus
//      两挂载（生产容器缺模型目录，二次修复）；
//   3. 调用链经 Git Bash→node 存在 MSYS 路径改写风险（本脚本全程 Node 内读
//      inspect JSON 文件 + spawnSync 直传参数数组、不经 shell 再解释，规避改写）。
//
// 继承规则（buildDockerRunArgs，纯函数返回参数数组、无 IO，单元测试直接断言）：
//   * restart policy：HostConfig.RestartPolicy.Name 为 'no'/空/缺失 → 固定
//     unless-stopped（硬编码 no 禁止回归）；always/unless-stopped 原样保留；
//     options.restartPolicy 显式传参可覆盖；
//   * Mounts[] 全量逐条 → -v（bind 用 Source；具名卷 Source 空时用 Name；
//     Mode 空且 RW===false → 补 :ro），extraMounts 只能追加、不可丢失继承项；
//   * HostConfig.PortBindings 逐端口 → -p（HostIp 作前缀；非 tcp 带协议后缀）；
//   * Config.Env 全量逐条 → -e；extraEnv 仅追加未继承键，不覆盖继承键；
//   * HostConfig.NetworkMode（非 'default'）→ --network（含 container:<name> 形态）；
//   * restart policy 不在 -e 域——由 --restart 参数显式传递。
//
// 用法：
//   node scripts/swap-console.cjs <inspect.json> <new-image> [--name NAME] [--dry-run]
//     inspect.json  `docker inspect <container> -f '{{json .}}'` 或 `docker inspect <container>`
//                   的输出（数组或单对象均可）
//     --name NAME   覆盖容器名（默认继承 inspect.Name，去前导 /）
//     --dry-run     只打印安全摘要（容器/镜像/policy/网络/端口/挂载/env 键名与数量；
//                   env 值与完整参数不回显），不调用 docker
// 实际替换序列：docker rm -f <旧容器名>（尽力而为）→ docker run -d <继承参数> <新镜像>。
// 安全红线：inspect 继承的 env 含生产秘密——任何路径（dry-run/执行/失败）都不得回显
// 完整参数或 env 值；失败输出仅允许 docker 自身 stderr（daemon 侧消息，不含调用参数）。
'use strict';

const fs = require('node:fs');
const { spawnSync } = require('node:child_process');
const path = require('node:path');

// 硬编码 no 的替代基线：daemon 重启自愈的最低要求（rc.11 缺陷回归锁）
const FALLBACK_RESTART_POLICY = 'unless-stopped';

// HostConfig.RestartPolicy.Name → 生效 policy：'no'/空/缺失 → unless-stopped，其余原样
function resolveRestartPolicy(inspect) {
  const name = inspect && inspect.HostConfig && inspect.HostConfig.RestartPolicy
    ? inspect.HostConfig.RestartPolicy.Name : undefined;
  if (!name || name === 'no') return FALLBACK_RESTART_POLICY;
  return name;
}

// inspect Mounts 条目 → `-v` 规格串；无法落挂载点时返回 null
function mountSpec(m) {
  if (!m || !m.Destination) return null;
  // Mode 为空但 RW===false（compose 只读挂载常见形态）→ 补 :ro，防只读语义丢失
  const mode = m.Mode || (m.RW === false ? 'ro' : '');
  const src = m.Source || m.Name; // bind 用 Source；具名卷 Source 空时用卷名
  if (!src) return m.Destination; // 匿名卷：仅声明挂载点
  return mode ? `${src}:${m.Destination}:${mode}` : `${src}:${m.Destination}`;
}

// HostConfig.PortBindings → ['-p' 规格串]；"4730/tcp" + {HostIp,HostPort} → "HostIp:HostPort:4730"
function portSpecs(portBindings) {
  const out = [];
  for (const [key, bindings] of Object.entries(portBindings || {})) {
    const slash = key.indexOf('/');
    const containerPort = slash === -1 ? key : key.slice(0, slash);
    const proto = slash === -1 ? 'tcp' : key.slice(slash + 1);
    const suffix = proto === 'udp' ? '/udp' : '';
    for (const b of bindings || []) {
      if (!b) continue;
      if (!b.HostPort) { out.push(`${containerPort}${suffix}`); continue; } // 随机宿主端口
      const hostPart = b.HostIp ? `${b.HostIp}:${b.HostPort}` : b.HostPort;
      out.push(`${hostPart}:${containerPort}${suffix}`);
    }
  }
  return out;
}

// "KEY=VALUE" → "KEY"；裸键原样
function envKey(e) {
  const i = e.indexOf('=');
  return i === -1 ? e : e.slice(0, i);
}

// inspect + 新镜像 → docker run 参数数组（不含 'run'/-d 等调用形态；image 居末）。纯函数、无 IO。
function buildDockerRunArgs(inspect, image, options = {}) {
  if (!inspect || typeof inspect !== 'object') throw new Error('inspect 必须是 docker inspect 的 JSON 对象');
  if (!image) throw new Error('image 必填');
  const hc = inspect.HostConfig || {};
  const cfg = inspect.Config || {};
  const args = [];

  const name = options.name != null ? options.name
    : (typeof inspect.Name === 'string' ? inspect.Name.replace(/^\//, '') : '');
  if (name) args.push('--name', name);

  const policy = options.restartPolicy != null ? options.restartPolicy : resolveRestartPolicy(inspect);
  if (policy) args.push('--restart', policy);

  if (hc.NetworkMode && hc.NetworkMode !== 'default') args.push('--network', hc.NetworkMode);

  for (const p of portSpecs(hc.PortBindings)) args.push('-p', p);

  for (const m of inspect.Mounts || []) {
    const spec = mountSpec(m);
    if (spec) args.push('-v', spec);
  }
  for (const extra of options.extraMounts || []) args.push('-v', extra); // 仅追加，不丢继承

  const inheritedEnv = (cfg.Env || []).filter((e) => !e.startsWith('MERGEPILOT_VERSION=') && !e.startsWith('MERGEPILOT_GIT_COMMIT='));
  // 版本烙印过滤（2026-10-13 rc.27 实证）：running 容器的 inspect env 含上一镜像烘焙的
  // MERGEPILOT_VERSION（image ENV 会并进 running env）——不过滤会覆盖新镜像的版本值，
  // 造成"代码 rc.27、标签 rc.26"。版本只随镜像走（构建期 ARG/ENV 唯一真源）。
  const inheritedKeys = new Set(inheritedEnv.map(envKey));
  for (const e of inheritedEnv) args.push('-e', e);
  for (const e of options.extraEnv || []) {
    if (inheritedKeys.has(envKey(e))) continue; // 继承键权威：extraEnv 不覆盖
    args.push('-e', e);
  }

  args.push(image);
  return args;
}

// 安全摘要（白名单输出）：容器名/镜像/policy/网络/端口/挂载/env 键名与数量。
// 完整 docker run 参数与 env 值永不回显——inspect 继承的 env 含生产秘密
// （session 密钥/私钥/连接串/API key），rc.18 切换实录（2026-10-08）曾经
// `+ docker run -d` + args.join 全量泄露到运维转录。纯函数，可直接断言。
function safeSwapSummary(inspect, image, { name, dryRun } = {}) {
  const containerName = name != null ? name
    : (typeof inspect.Name === 'string' ? inspect.Name.replace(/^\//, '') : '');
  const hc = inspect.HostConfig || {};
  const policy = hc.RestartPolicy && hc.RestartPolicy.Name ? hc.RestartPolicy.Name
    : FALLBACK_RESTART_POLICY;
  const net = hc.NetworkMode && hc.NetworkMode !== 'default' ? hc.NetworkMode : 'default';
  const ports = portSpecs(hc.PortBindings);
  const mounts = (inspect.Mounts || []).map((m) => mountSpec(m)).filter(Boolean);
  const envKeys = ((inspect.Config && inspect.Config.Env) || []).map(envKey).sort();
  // 自由字符串脱敏：镜像引用剥离 userinfo（user:pass@registry 形态不得回显）
  const safeImage = String(image).replace(/^[^/@]+@/, '');
  return [
    `${dryRun ? '[DRY-RUN] ' : ''}docker run -d（完整参数不回显——继承 env 含生产秘密）`,
    `  container=${containerName || '—'}`,
    `  image=${safeImage}`,
    `  restart=${policy}  network=${net}`,
    `  ports=${ports.length ? ports.join(' ') : '—'}`,
    `  mounts=${mounts.length ? mounts.join(' ') : '—'}`,
    `  env=${envKeys.length} 项（仅键名，值不回显）：${envKeys.length ? envKeys.join(', ') : '—'}`,
  ].join('\n');
}

// ---- 主入口（实际 swap 执行）：真实路径 spawnSync('docker', …)，测试经 deps.exec mock 注入 ----

function readInspect(file) {
  let raw;
  try { raw = fs.readFileSync(file, 'utf8'); }
  catch (e) { throw new Error(`无法读取 inspect 文件 ${file}: ${e.message}`); }
  let parsed;
  try { parsed = JSON.parse(raw); }
  catch (e) { throw new Error(`inspect JSON 解析失败 (${file}): ${e.message}`); }
  if (Array.isArray(parsed)) parsed = parsed[0]; // docker inspect 默认输出数组
  if (!parsed || typeof parsed !== 'object' || !parsed.HostConfig) {
    throw new Error('inspect JSON 缺少 HostConfig——请提供 docker inspect <container> 的完整输出');
  }
  return parsed;
}

function parseArgv(argv) {
  const positional = [];
  let name;
  let dryRun = false;
  let expectCommit = null;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') { dryRun = true; continue; }
    if (a === '--name') { name = argv[++i]; continue; }
    if (a.startsWith('--name=')) { name = a.slice('--name='.length); continue; }
    if (a === '--expect-commit') { expectCommit = argv[++i]; continue; }
    if (a.startsWith('--expect-commit=')) { expectCommit = a.slice('--expect-commit='.length); continue; }
    positional.push(a);
  }
  return { positional, name, dryRun, expectCommit };
}

// 返回进程退出码；deps.exec/deps.log 可注入（真实路径 = spawnSync + stdout）
async function swapConsole(argv, deps = {}) {
  const exec = deps.exec || ((args) => spawnSync('docker', args, { encoding: 'utf8' }));
  const log = deps.log || ((m) => process.stdout.write(m + '\n'));
  const { positional, name, dryRun, expectCommit } = parseArgv(argv);
  const [inspectFile, image] = positional;
  if (!inspectFile || !image) {
    log('用法: node scripts/swap-console.cjs <inspect.json> <new-image> [--name NAME] [--dry-run]');
    return 2;
  }
  const inspect = readInspect(inspectFile);
  const args = buildDockerRunArgs(inspect, image, { name });
  const containerName = name != null ? name : (typeof inspect.Name === 'string' ? inspect.Name.replace(/^\//, '') : '');

  // 安全输出：只打白名单摘要（env 仅键名+数量）——完整参数含生产秘密，永不回显
  log(safeSwapSummary(inspect, image, { name, dryRun }));
  if (dryRun) return 0;

  // ── 前置检查 1：Matrix 现役凭据校验（2026-10-13 rc.26 切换实证：继承 env 口令可能陈旧
//    → 四角色 MT_LOGIN_HTTP_403 → run BLOCKED）。在【旧容器内】用将被继承的 env 实际登录一次：
//    200=通过；非 200=切换中止（exit 3），不触碰旧容器，不回显凭据。无 Matrix env 的部署自动跳过。
  const inspectEnv = inspect.Config.Env ?? [];
  const hasMatrixEnv = inspectEnv.some((e) => e.startsWith('MU_AGENTTEAMS_MATRIX_USER='))
    && inspectEnv.some((e) => e.startsWith('MU_AGENTTEAMS_MATRIX_PASSWORD='));
  if (containerName && hasMatrixEnv && !deps.skipMatrixCheck) {
    const loginScript = "const MU = process.env.MU_AGENTTEAMS_MATRIX_URL; const base = MU.endsWith('/') ? MU.slice(0, -1) : MU; fetch(base + '/_matrix/client/v3/login',"
      + "{method:'POST',headers:{'content-type':'application/json'},"
      + "body:JSON.stringify({type:'m.login.password',user:process.env.MU_AGENTTEAMS_MATRIX_USER,password:process.env.MU_AGENTTEAMS_MATRIX_PASSWORD})})"
      + ".then(r=>console.log('STATUS '+r.status)).catch(e=>console.log('STATUS ERR '+e.message))";
    const pre = exec(['exec', containerName, 'node', '-e', loginScript]);
    const preStatus = String((pre.stdout || '')).match(/STATUS (\d{3}|ERR \S+)/) ?? [];
    const ok200 = /STATUS 200/.test(pre.stdout || '');
    if (pre.error || !ok200) {
      log('前置 Matrix 凭据校验未通过（继承 env 登录非 200）——切换中止（旧容器未受影响）。');
      log('处置：将 MU_AGENTTEAMS_MATRIX_PASSWORD 收敛到现役值后重试本工具。');
      return 3;
    }
    log('前置 Matrix 凭据校验通过（登录 200，凭据不回显）');
  }

  // ── 前置检查 2 记录：待连接的非默认网络（创建后 connect；bridge 除外）──
  const extraNetworks = Object.keys(inspect.NetworkSettings?.Networks ?? {})
    .filter((n) => n && n !== 'bridge');
  if (extraNetworks.length) log('将继承非默认网络: ' + extraNetworks.join(', '));

  if (containerName) {
    // 尽力而为移除旧容器：不存在时 docker 报非零，忽略之；仍占用时 run 步骤会显式失败
    exec(['rm', '-f', containerName]);
  }
  const run = exec(['run', '-d', ...args]);
  if (run.error) {
    // 异常对象不外抛（避免携带参数/env 的多行负载进入转录）——只留单行首句
    const msg = String(run.error.message || 'spawn 失败').split('\n')[0];
    throw new Error('docker run 无法启动：' + msg);
  }
  if (run.status !== 0) {
    // 对抗性红线：docker stderr 内容不可信（本轮验收要求模拟 stderr 含秘密/
    // 连接串/多行私钥的场景）——绝不透传 stderr 原文，只报退出码与诊断指引
    log(`docker run 失败（exit=${run.status}）——本工具不回显错误详情以防凭据进入转录；` +
        `可查看 docker daemon 日志或手动重跑同参数诊断`);
    return run.status || 1;
  }
  log('container=' + (run.stdout || '').trim());

  // ── 创建后：非默认网络逐个 connect（swap 只继承 NetworkMode——漏挂第二网络会失去
//    自定义 DNS（beta-mp-pg 解析），rc.25/rc.26 两轮生产同款坑）──
  for (const net of extraNetworks) {
    const c = exec(['network', 'connect', net, containerName]);
    log(`network connect ${net}: ${c.status === 0 ? 'ok' : 'exit=' + c.status + '（可能已连接，忽略）'}`);
  }

  // ── 后置检查 1：健康+依赖可达（data_mode=live 表示 PG 实时可读；snapshot=依赖断）──
  const oldImage = inspect.Config.Image || image;
  // data_mode=live 断言仅适用于 PG 部署（无 DSN 的部署 health 本就不含 live 语义）
  const hasPgDsn = inspectEnv.some((e) => e.startsWith('CONSOLE_PG_DSN='));
  const rollback = (why) => {
    log('后置检查未通过（' + why + '）——回滚到旧镜像 ' + oldImage.slice(0, 40));
    if (containerName) exec(['rm', '-f', containerName]);
    const oldArgs = buildDockerRunArgs(inspect, oldImage, { name: containerName || undefined });
    exec(['run', '-d', ...oldArgs]);
    // 回滚容器同样补挂非默认网络（rc.35.1 切换实测缺陷：回滚只 run 不 connect
    // → 旧容器失去自定义 DNS（beta-mp-pg/ctrl 解析）→ Matrix/DB 全挂）
    for (const net of extraNetworks) {
      const c = exec(['network', 'connect', net, containerName]);
      log(`回滚容器 network connect ${net}: ${c.status === 0 ? 'ok' : 'exit=' + c.status}`);
    }
    log('回滚完成（旧容器已按原配置重建）。退出码 1。');
    return 1;
  };
  // 健康探针源码=scripts/swap-health-probe.js（独立文件，fs 读入后经 exec 注入——
  // 内联字符串经多层转义往返被写坏、且 node 运行时 SyntaxError 走 stderr 被静默
  // 丢弃的教训见该文件头注释；rc.35.2 排障终局实录）。
  const probeSrc = fs.readFileSync(path.join(__dirname, 'swap-health-probe.js'), 'utf8');
  let live = !hasPgDsn; // 无 PG DSN：无需 live 断言（视为通过）
  for (let i = 0; !live && i < 12; i++) {
    if (containerName) {
      const h = exec(['exec', containerName, 'node', '-e', probeSrc]);
      const out = String(h.stdout || '') + (h.status !== 0 ? ' [exit=' + h.status + ' err=' + String(h.stderr || '').slice(0, 160) + ']' : '');
      log('健康检查第 ' + (i + 1) + '/12 轮: ' + (out.trim() ? out.trim().slice(0, 170) : '（exec 无输出）'));
      if (/HEALTH live/.test(out)) { live = true; log('健康检查通过（data_mode=live）: ' + out.replace('HEALTH ', '').trim()); break; }
      if (/HEALTH /.test(out)) log('健康检查: ' + out.replace('HEALTH ', '').trim() + '（等待依赖就绪）');
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }
  if (!live) {
    // 失败诊断（rc.35 首切实测）：抓新容器启动日志尾部辅助定位（启动日志无秘密；
    // 对抗性红线仍由下方 rollback 不透传 docker run stderr 承担）
    const dl = containerName ? exec(['logs', '--tail', '20', containerName]) : null;
    const tail = String((dl && dl.stdout) || '').trim();
    if (tail) log('失败容器日志（末 20 行）：' + tail.slice(-1200));
    return rollback('data_mode 未达 live（DB/依赖不可达或启动失败）');
  }

  // ── 后置检查 1b：构建身份核对（发布产物一致性波）——期望 commit 不符=回滚 ──
  if (expectCommit && containerName) {
    const idScript = "fetch('http://127.0.0.1:' + (process.env.CONSOLE_PORT || 4730) + '/api/health')"
      + ".then(r=>r.json()).then(j=>console.log('ID ' + (j.git_commit ?? 'null') + ' ' + (j.frontend_bundle ? j.frontend_bundle.file : 'null')))"
      + ".catch(e=>console.log('ID ERR'))";
    const idr = exec(['exec', containerName, 'node', '-e', idScript]);
    const m = String(idr.stdout || '').match(/ID ([0-9a-f]{40}|null) (.+)/);
    const got = m ? m[1] : null;
    if (got !== expectCommit) {
      return rollback(`构建身份不符：health.git_commit=${got ?? '读取失败'}，期望 ${expectCommit.slice(0, 12)}…——镜像与源码可能不一致`);
    }
    log('构建身份核对通过（git_commit=' + got.slice(0, 12) + '…）');
  }

  // ── 后置检查 2：新容器内 Matrix 实际登录（与前置同口径，防环境注入差异）──
  if (hasMatrixEnv && !deps.skipMatrixCheck) {
    const loginScript2 = "const MU = process.env.MU_AGENTTEAMS_MATRIX_URL; const base = MU.endsWith('/') ? MU.slice(0, -1) : MU; fetch(base + '/_matrix/client/v3/login',"
      + "{method:'POST',headers:{'content-type':'application/json'},"
      + "body:JSON.stringify({type:'m.login.password',user:process.env.MU_AGENTTEAMS_MATRIX_USER,password:process.env.MU_AGENTTEAMS_MATRIX_PASSWORD})})"
      + ".then(r=>console.log('STATUS '+r.status)).catch(e=>console.log('STATUS ERR '+e.message))";
    const post = exec(['exec', containerName, 'node', '-e', loginScript2]);
    if (!/STATUS 200/.test(post.stdout || '')) {
      return rollback('新容器 Matrix 登录非 200');
    }
    log('后置 Matrix 登录校验通过（200）');
  }
  log('SWAP OK');
  return 0;
}

module.exports = { FALLBACK_RESTART_POLICY, resolveRestartPolicy, mountSpec, portSpecs, buildDockerRunArgs, safeSwapSummary, swapConsole };

if (require.main === module) {
  try { swapConsole(process.argv.slice(2)).then((c) => process.exit(c)).catch((e) => { console.error('swap-console: ' + (e && e.message ? e.message : e)); process.exit(1); }); }
  catch (e) { console.error('swap-console: ' + (e && e.message ? e.message : e)); process.exit(1); }
}
