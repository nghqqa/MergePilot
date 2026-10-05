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
//     --dry-run     只打印将执行的 docker 命令，不调用 docker
// 实际替换序列：docker rm -f <旧容器名>（尽力而为）→ docker run -d <继承参数> <新镜像>。
'use strict';

const fs = require('node:fs');
const { spawnSync } = require('node:child_process');

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

  const inheritedEnv = cfg.Env || [];
  const inheritedKeys = new Set(inheritedEnv.map(envKey));
  for (const e of inheritedEnv) args.push('-e', e);
  for (const e of options.extraEnv || []) {
    if (inheritedKeys.has(envKey(e))) continue; // 继承键权威：extraEnv 不覆盖
    args.push('-e', e);
  }

  args.push(image);
  return args;
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
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') { dryRun = true; continue; }
    if (a === '--name') { name = argv[++i]; continue; }
    if (a.startsWith('--name=')) { name = a.slice('--name='.length); continue; }
    positional.push(a);
  }
  return { positional, name, dryRun };
}

// 返回进程退出码；deps.exec/deps.log 可注入（真实路径 = spawnSync + stdout）
function swapConsole(argv, deps = {}) {
  const exec = deps.exec || ((args) => spawnSync('docker', args, { encoding: 'utf8' }));
  const log = deps.log || ((m) => process.stdout.write(m + '\n'));
  const { positional, name, dryRun } = parseArgv(argv);
  const [inspectFile, image] = positional;
  if (!inspectFile || !image) {
    log('用法: node scripts/swap-console.cjs <inspect.json> <new-image> [--name NAME] [--dry-run]');
    return 2;
  }
  const inspect = readInspect(inspectFile);
  const args = buildDockerRunArgs(inspect, image, { name });
  const containerName = name != null ? name : (typeof inspect.Name === 'string' ? inspect.Name.replace(/^\//, '') : '');

  log('+ docker run -d ' + args.join(' '));
  if (dryRun) return 0;

  if (containerName) {
    // 尽力而为移除旧容器：不存在时 docker 报非零，忽略之；仍占用时 run 步骤会显式失败
    exec(['rm', '-f', containerName]);
  }
  const run = exec(['run', '-d', ...args]);
  if (run.error) throw run.error;
  if (run.status !== 0) {
    log((run.stderr || 'docker run 失败').trim());
    return run.status || 1;
  }
  log((run.stdout || '').trim());
  return 0;
}

module.exports = { FALLBACK_RESTART_POLICY, resolveRestartPolicy, mountSpec, portSpecs, buildDockerRunArgs, swapConsole };

if (require.main === module) {
  try { process.exit(swapConsole(process.argv.slice(2))); }
  catch (e) { console.error('swap-console: ' + (e && e.message ? e.message : e)); process.exit(1); }
}
