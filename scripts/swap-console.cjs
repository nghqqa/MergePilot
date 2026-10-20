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

// ── 部署前 inspect 完整性校验（2026-10-18 rc3 换版事故回归锁）──────────────
// 事故：运维侧把【掩码后的展示副本】当作部署输入（DSN 密码='***'），swap 照常
// 执行 → 新容器 PG 断连。修复：部署前拒绝疑似脱敏/缺项的 inspect——在停止旧容器
// 之前失败（fail-fast），旧容器零影响。脱敏记录只允许用于展示/归档，不得进入部署。
const REDACT_MARKERS = ['***', 'redacted', '<masked>'];
const SECRET_KEY_RE = /(PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY|PRIVATE_KEY|_KEY|KEY_|PAT|DSN)/i;
function envValueLooksRedacted(key, value) {
  if (!SECRET_KEY_RE.test(key)) return false;
  const v = String(value ?? '');
  if (!v) return false; // 空值≠脱敏：可能是合法留空的可选配置（调用方降级为警告）
  return REDACT_MARKERS.some((m) => v.toLowerCase().includes(m));
}
function validateForDeploy(inspect) {
  const reasons = [];
  const warnings = [];
  const env = (inspect && inspect.Config && Array.isArray(inspect.Config.Env)) ? inspect.Config.Env : [];
  for (const e of env) {
    const eq = e.indexOf('=');
    if (eq <= 0) continue;
    const key = e.slice(0, eq), value = e.slice(eq + 1);
    if (!SECRET_KEY_RE.test(key)) continue;
    const v = String(value ?? '');
    if (v && REDACT_MARKERS.some((m) => v.toLowerCase().includes(m))) {
      // 掩码标记=展示副本特征，部署拒绝（rc3 事故主因）
      reasons.push(key + ' 值疑似脱敏占位——这是展示副本，不是可部署配置');
    } else if (!v) {
      // 空值=可能是合法留空的可选配置（如未启用 Gitee 时的 PAT）——仅警告不拒
      warnings.push(key + ' 为空——若该功能启用中，部署后对应能力不可用（如实提示，不阻断）');
    }
  }
  const isMultiuser = env.some((e) => e === 'MU_MODE=multiuser');
  if (isMultiuser && !env.some((e) => e.startsWith('CONSOLE_PG_DSN='))) {
    reasons.push('MU_MODE=multiuser 但缺少 CONSOLE_PG_DSN——多用户部署必需');
  }
  return { ok: reasons.length === 0, reasons, warnings };
}

// ── Matrix 登录体（纯函数）：identifier 格式──────────────────────────────
// 2026-10-18 rc3 换版实证：当前 Tuwunel 对旧式顶层 user 字段返回 403（同凭据
// identifier 格式 200）——统一 identifier:{type:'m.id.user',user}。
// 注意：实际登录脚本在【容器内】以 env 构造（见 swapConsole 内 loginScript），
// 本函数为格式契约+单测锚——两处实现必须同步修改。
function matrixLoginBody(user, password) {
  return JSON.stringify({ type: 'm.login.password',
    identifier: { type: 'm.id.user', user: String(user ?? '') },
    password: String(password ?? '') });
}

// ── 部署输入 vs 实际运行容器比对（纯函数；rc3 事故回归锁）────────────────
// 事故：pre-rc1 陈旧 inspect（Image=rc.35.5 时代 tag、env 键集旧）被当作部署输入。
// 修复：与 live 容器实际 inspect 比对——镜像身份不一致或 env 键集缺失=陈旧/部分
// 丢失，在停止旧容器【之前】拒绝。file 侧新增键或修改值（如试点 scope 变更）合法放行。
function compareWithLive(fileInspect, liveInspect) {
  if (!liveInspect) return { ok: true, reasons: [] }; // 无同名词容器=非替换场景，跳过
  const reasons = [];
  const fileImage = pickRollbackImage(fileInspect, null);
  const liveImage = pickRollbackImage(liveInspect, null);
  if (fileImage && liveImage && fileImage !== liveImage) {
    reasons.push('镜像身份不一致——inspect 已陈旧（file=' + String(fileImage).slice(0, 20)
      + '… live=' + String(liveImage).slice(0, 20) + '…）。请重新 docker inspect 现容器生成部署输入');
  }
  const fileKeys = new Set((fileInspect?.Config?.Env ?? []).map((e) => e.split('=', 1)[0]));
  const liveKeys = (liveInspect?.Config?.Env ?? []).map((e) => e.split('=', 1)[0]);
  const missing = liveKeys.filter((k) => !fileKeys.has(k));
  if (missing.length) {
    reasons.push('env 键集缺失 ' + missing.length + ' 项（如 ' + missing.slice(0, 3).join(',') + '…）——inspect 可能来自旧版本容器');
  }
  return { ok: reasons.length === 0, reasons };
}

// ── 回滚镜像选择（纯函数）：优先不可变 Image ID────────────────────────────
// 事故：回滚用 inspect.Config.Image（可变 tag，且可能来自陈旧 inspect）——rc3 换版
// 回滚复活了 rc.35.5 旧 tag。修复：回滚目标=切换前旧容器的实际不可变镜像 ID
// （inspect.Image=sha256:…）；Config.Image 仅作兜底。
function pickRollbackImage(inspect, fallbackImage) {
  return (inspect && inspect.Image) ? inspect.Image
    : ((inspect && inspect.Config && inspect.Config.Image) || fallbackImage);
}

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
  let skipMatrixCheck = false;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') { dryRun = true; continue; }
    if (a === '--name') { name = argv[++i]; continue; }
    if (a.startsWith('--name=')) { name = a.slice('--name='.length); continue; }
    if (a === '--expect-commit') { expectCommit = argv[++i]; continue; }
    if (a.startsWith('--expect-commit=')) { expectCommit = a.slice('--expect-commit='.length); continue; }
    if (a === '--skip-matrix-check') { skipMatrixCheck = true; continue; }
    positional.push(a);
  }
  return { positional, name, dryRun, expectCommit, skipMatrixCheck };
}

// 返回进程退出码；deps.exec/deps.log 可注入（真实路径 = spawnSync + stdout）
async function swapConsole(argv, deps = {}) {
  const exec = deps.exec || ((args) => spawnSync('docker', args, { encoding: 'utf8' }));
  const log = deps.log || ((m) => process.stdout.write(m + '\n'));
  const { positional, name, dryRun, expectCommit, skipMatrixCheck } = parseArgv(argv);
  const [inspectFile, image] = positional;
  if (!inspectFile || !image) {
    log('用法: node scripts/swap-console.cjs <inspect.json> <new-image> [--name NAME] [--dry-run]');
    return 2;
  }
  const inspect = readInspect(inspectFile);
  // ── 前置检查 0：部署完整性（掩码/缺项拒绝）——先于一切破坏性动作 ──
  const deployCheck = validateForDeploy(inspect);
  if (!deployCheck.ok) {
    log('部署前校验未通过——inspect 疑似脱敏/缺项，拒绝执行（旧容器未受影响）：');
    for (const r of deployCheck.reasons) log('  - ' + r);
    for (const w of (deployCheck.warnings ?? [])) log('  ⚠ ' + w);
    log('处置：改用未脱敏的恢复配置（docker inspect 原始输出）；脱敏副本仅用于展示/归档。');
    return 4;
  }
  for (const w of (deployCheck.warnings ?? [])) log('  ⚠ ' + w);
  const args = buildDockerRunArgs(inspect, image, { name });
  const containerName = name != null ? name : (typeof inspect.Name === 'string' ? inspect.Name.replace(/^\//, '') : '');

  // 安全输出：只打白名单摘要（env 仅键名+数量）——完整参数含生产秘密，永不回显
  log(safeSwapSummary(inspect, image, { name, dryRun }));
  if (dryRun) return 0;

  // ── 前置检查 1：执行器现役凭据校验（rc.26 切换实证：继承 env 口令可能陈旧 → run BLOCKED）。
//    在【旧容器内】用将被继承的 env 按实际认证形状校验（2026-10-20 形状分派修复）：
//    A. Matrix 口令（USER+PASSWORD 均非空）→ /_matrix/client/v3/login（identifier 格式）；
//    B. TOKEN（口令缺失/为空 + MU_AGENTTEAMS_TOKEN + BASE_URL）→ GET /api/v1/workers
//       （Bearer——与 agentteams-executor/runtime-readiness 同一只读身份校验端点）；
//    C. 均未配置 → MU_EXECUTOR=agentteams 时 EXECUTOR_MISMATCH（阻断）；否则
//       UNCONFIGURED（可选依赖未配置——如实放行并注明，不冒充认证通过）。
//    结果四类：OK / AUTH（401/403）/ NETWORK（DNS/连接失败）/ SERVICE（其余非 2xx）；
//    AUTH/NETWORK/SERVICE 均阻断（exit 3），消息区分类别；凭据不回显。
//    deps.skipMatrixCheck 或 --skip-matrix-check = 显式跳过（响亮记录，不构成认证通过证据）。
  const inspectEnv = inspect.Config.Env ?? [];
  const envOf = (k) => { const row = inspectEnv.find((e) => e.startsWith(k + '=')); return row == null ? null : row.slice(k.length + 1); };
  const hasAuthEnv = ['MU_AGENTTEAMS_MATRIX_USER', 'MU_AGENTTEAMS_MATRIX_PASSWORD',
    'MU_AGENTTEAMS_TOKEN', 'MU_AGENTTEAMS_BASE_URL'].some((k) => envOf(k) != null);
  const skipMatrix = deps.skipMatrixCheck === true || skipMatrixCheck;
  // 形状分派探针（前置=旧容器/后置=新容器共用；按将被继承的 env 实际认证形状校验）。
  const runAuthProbe = (targetContainer) => {
    const precheckScript = [
      "const e=process.env;const mu=(e.MU_AGENTTEAMS_MATRIX_USER??'').trim();const mp=(e.MU_AGENTTEAMS_MATRIX_PASSWORD??'').trim();",
      "const tok=(e.MU_AGENTTEAMS_TOKEN??'').trim();const bu=(e.MU_AGENTTEAMS_BASE_URL??'').trim().replace(/\\/+$/,'');",
      "const cls=(x,d)=>console.log('PRECHECK '+x+' '+d);",
      "(async()=>{",
      " if(mu&&mp){try{const r=await fetch((e.MU_AGENTTEAMS_MATRIX_URL??'').replace(/\\/+$/,'')+'/_matrix/client/v3/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({type:'m.login.password',identifier:{type:'m.id.user',user:mu},password:mp})});",
      "  cls(r.status===200?'OK':(r.status===401||r.status===403?'AUTH':'SERVICE'),'matrix-login '+r.status);}catch(err){cls('NETWORK','matrix '+(err&&err.cause&&err.cause.code?err.cause.code:err.message).slice(0,60));}return;}",
      " if(tok&&bu){try{const r=await fetch(bu+'/api/v1/workers',{headers:{Authorization:'Bearer '+tok}});",
      "  cls(r.status===200?'OK':(r.status===401||r.status===403?'AUTH':'SERVICE'),'token-workers '+r.status);}catch(err){cls('NETWORK','token '+(err&&err.cause&&err.cause.code?err.cause.code:err.message).slice(0,60));}return;}",
      " if((e.MU_EXECUTOR??'')==='agentteams'){cls('EXECUTOR_MISMATCH','executor=agentteams 但 Matrix 口令与 TOKEN 均不可用');return;}",
      " cls('UNCONFIGURED','agentteams 认证未配置（executor 非强制）——可选依赖，如实放行');",
      "})().catch(err=>cls('NETWORK','probe '+err.message));",
    ].join('');
    const pre = exec(['exec', targetContainer, 'node', '-e', precheckScript]);
    const m = String(pre.stdout || '').match(/PRECHECK (OK|AUTH|NETWORK|SERVICE|EXECUTOR_MISMATCH|UNCONFIGURED) ?(.*)/) ?? [];
    return { verdict: m[1] || null, detail: (m[2] || '').slice(0, 80), probeError: pre.error || null };
  };
  const authFailMsg = (verdict, detail) => {
    if (verdict === 'AUTH') {
      log('执行器凭据校验：认证失败（' + detail + '）。处置：将 MU_AGENTTEAMS_MATRIX_PASSWORD 或 MU_AGENTTEAMS_TOKEN 收敛到现役值后重试。');
    } else if (verdict === 'NETWORK') {
      log('执行器凭据校验：网络/DNS 失败（' + detail + '）。处置：确认目标服务可达（Matrix URL / MU_AGENTTEAMS_BASE_URL）后重试。');
    } else if (verdict === 'SERVICE') {
      log('执行器凭据校验：服务端异常（' + detail + '）。处置：确认执行器服务健康后重试。');
    } else {
      log('执行器凭据校验：配置与执行器不匹配（' + detail + '）。处置：为 agentteams 执行器提供可用认证（Matrix 口令或 MU_AGENTTEAMS_TOKEN）。');
    }
  };
  if (containerName && hasAuthEnv && !skipMatrix) {
    const { verdict, detail, probeError } = runAuthProbe(containerName);
    if (probeError || !verdict) {
      log('前置执行器凭据校验无结果（探针未返回）——切换中止（旧容器未受影响）。');
      if (probeError) log('  探针执行错误：' + String(probeError.message ?? probeError).slice(0, 120));
      return 3;
    }
    if (verdict === 'OK') {
      log('前置执行器凭据校验通过（' + detail + '，凭据不回显）');
    } else if (verdict === 'UNCONFIGURED') {
      log('前置执行器凭据校验：可选依赖未配置（' + detail + '）——不构成认证通过证据。');
    } else {
      log('前置校验中止（旧容器未受影响）。');
      authFailMsg(verdict, detail);
      return 3;
    }
  }
  if (containerName && hasAuthEnv && skipMatrix) {
    log('⚠ 执行器凭据校验已被显式跳过（--skip-matrix-check/deps）——本次部署不构成认证通过证据，切换后请另行核验执行器认证。');
  }

  // ── 前置检查 1.5：部署输入 vs 实际运行容器比对（rc3 事故回归锁）────────
  // 陈旧 inspect（镜像身份/env 键集不匹配）在停止旧容器【之前】拒绝。
  // 回滚锚以【live 实际 inspect】为准——文件仅作配置变更载体。
  let liveInspect = null;
  if (containerName) {
    const li = exec(['inspect', containerName]);
    try {
      const parsed = JSON.parse(String(li.stdout || 'null'));
      liveInspect = Array.isArray(parsed) ? (parsed[0] ?? null) : parsed;
    } catch { liveInspect = null; }
    const cmp = compareWithLive(inspect, liveInspect);
    if (!cmp.ok) {
      log('部署输入与运行容器不一致——疑似陈旧/部分丢失的 inspect，拒绝执行（旧容器未受影响）：');
      for (const r of cmp.reasons) log('  - ' + r);
      log('处置：docker inspect ' + containerName + ' 重新生成部署输入；如需变更 env 请在【新】输入上修改。');
      return 5;
    }
    log('部署输入与运行容器比对通过（镜像身份一致' + (liveInspect ? '；回滚锚=' + String(pickRollbackImage(liveInspect, image)).slice(0, 20) + '…' : '') + '）');
  }

  // ── 前置检查 1.6：回滚镜像可用性预检（rc3 隔离测试 C 二层缺陷回归锁）────
  // 事故：回滚时才发现本地无此镜像（untagged 被清理）→ run 静默失败 → 服务下线。
  // 修复：在停止旧容器之前确认回滚镜像在本地可解析；解析结果与原镜像身份一致性
  // 决定 Config.Image 兜底是否被允许（漂移 tag 不得恢复）。
  const originalImageId = liveInspect ? pickRollbackImage(liveInspect, image) : pickRollbackImage(inspect, image);
  const rbResolve = exec(['image', 'inspect', '--format', '{{.Id}}', originalImageId]);
  const rollbackResolvedId = String(rbResolve.stdout || '').trim();
  if (rbResolve.status !== 0 || !rollbackResolvedId) {
    log('回滚镜像不可用（本地无法解析 ' + originalImageId.slice(0, 20) + '…）——拒绝执行（旧容器未受影响）。');
    log('处置：docker pull/retag 恢复该镜像，或改用任一已验证可用版本作为回滚目标后重试。');
    return 6;
  }
  log('回滚镜像可用（' + rollbackResolvedId.slice(0, 20) + '…）');
  // 派生声明（run 之前就位——run 失败路径的 rollbackFn 需要，避免 TDZ）
  const hasPgDsn = inspectEnv.some((e) => e.startsWith('CONSOLE_PG_DSN='));
  const isMultiuserEnv = inspectEnv.some((e) => e === 'MU_MODE=multiuser');

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
    // rc3 事故回归锁：旧容器已被移除，run 失败不得裸退——尝试回滚到切换前
    // 身份（内部含回滚后就绪校验；再失败给出 🛑 运维指引），退出码归一为 1。
    return await rollbackFn('新容器启动失败（docker run exit=' + run.status + '）');
  }
  log('container=' + (run.stdout || '').trim());

  // ── 创建后：非默认网络逐个 connect（swap 只继承 NetworkMode——漏挂第二网络会失去
//    自定义 DNS（beta-mp-pg 解析），rc.25/rc.26 两轮生产同款坑）──
  for (const net of extraNetworks) {
    const c = exec(['network', 'connect', net, containerName]);
    log(`network connect ${net}: ${c.status === 0 ? 'ok' : 'exit=' + c.status + '（可能已连接，忽略）'}`);
  }

  // ── 后置检查 1：健康+依赖可达（data_mode=live 表示 PG 实时可读；snapshot=依赖断）──
  // 回滚目标=切换前旧容器的实际不可变镜像 ID（rc3 事故回归锁：Config.Image 可能是
  // 陈旧 inspect 里的可变 tag——回滚曾复活 rc.35.5 旧镜像）
  // data_mode=live 断言仅适用于 PG 部署（无 DSN 的部署 health 本就不含 live 语义）
  async function rollbackFn(why) {
    const oldImage = originalImageId;
    log('后置检查未通过（' + why + '）——回滚到旧镜像 ' + oldImage.slice(0, 40));
    if (containerName) exec(['rm', '-f', containerName]);
    // Config.Image 兜底身份门（rc3 事故回归锁）：仅当其解析出的镜像 ID 与切换前
    // 实际运行身份一致时才允许——漂移的旧 tag（如 rc.35.5 复活）一律拒绝。
    const fallbackRef = (inspect.Config.Image && inspect.Config.Image !== oldImage) ? inspect.Config.Image : null;
    let fallbackAllowed = false;
    if (fallbackRef) {
      const fr = exec(['image', 'inspect', '--format', '{{.Id}}', fallbackRef]);
      const fallbackId = String(fr.stdout || '').trim();
      fallbackAllowed = fr.status === 0 && !!fallbackId && fallbackId === oldImage;
    }
    const startRollbackImage = async (imgRef) => {
      const oldArgs = buildDockerRunArgs(inspect, imgRef, { name: containerName || undefined });
      exec(['run', '-d', ...oldArgs]);
      for (const net of extraNetworks) {
        const c = exec(['network', 'connect', net, containerName]);
        log(`回滚容器 network connect ${net}: ${c.status === 0 ? 'ok' : 'exit=' + c.status}`);
      }
      // 回滚成功判定（rc3 事故回归锁）：镜像身份一致 + 容器 Running + 应用健康
      // （live/mu 门同口径）——不能只验容器存在。
      const imgChk = exec(['inspect', '--format', '{{.Image}}|{{.State.Running}}', containerName]);
      const [runImg, runState] = String(imgChk.stdout || '').trim().split('|');
      if (runState !== 'true' || (runImg && runImg !== imgRef && !imgRef.startsWith('sha256:'))) {
        log('🛑 回滚启动异常（容器未运行或镜像身份不符）——服务可能处于下线状态，需运维立即介入。');
        return false;
      }
      if (hasPgDsn) {
        const probeSrc2 = fs.readFileSync(path.join(__dirname, 'swap-health-probe.js'), 'utf8');
        let rbLive = !hasPgDsn;
        for (let i = 0; !rbLive && i < 6; i++) {
          const h = exec(['exec', containerName, 'node', '-e', probeSrc2]);
          const out = String(h.stdout || '');
          log('回滚健康检查 ' + (i + 1) + '/6: ' + (out.trim() || '（exec 无输出）').slice(0, 150));
          if (/HEALTH live/.test(out) && !(isMultiuserEnv && /mu_ready=false/.test(out))) { rbLive = true; break; }
          if (i < 5) await new Promise((r) => setTimeout(r, 1000));
        }
        if (!rbLive) {
          log('🛑 回滚容器健康未达 live/mu_ready（数据库或依赖未就绪）——需运维立即介入核查。');
          return false;
        }
      }
      log('回滚完成（镜像身份 ' + String(runImg || imgRef).slice(0, 20) + '… 与切换前一致，健康检查通过）。');
      return true;
    };
    // 首选：切换前实际运行的不可变镜像 ID（精确）。
    if (await startRollbackImage(oldImage)) return 1;
    // ID 失败 → Config.Image 兜底【仅当其解析身份与原镜像一致】——漂移 tag 拒绝。
    if (fallbackRef && fallbackAllowed) {
      log('⚠️ 不可变镜像 ID 启动失败——尝试 Config.Image 兜底（解析身份已核对与原镜像一致）：' + fallbackRef.slice(0, 40));
      if (await startRollbackImage(fallbackRef)) return 1;
    } else if (fallbackRef) {
      log('🛑 Config.Image 兜底被拒绝（' + fallbackRef.slice(0, 40) + ' 解析身份与切换前实际运行镜像不一致——不得恢复可能漂移的旧 tag）。');
    }
    log('🛑 回滚未成功——当前服务处于下线状态，需要运维立即介入：');
    log('   docker pull/retag 恢复镜像后手动 docker run（参数见 inspect JSON），或切到任一已验证可用版本。');
    return 1;
  }
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
      if (/HEALTH live/.test(out)) {
        // rc3 事故回归锁：data_mode=live 之外，multiuser 部署还要求 mu_schema_ready=true
        // （掩码 DSN 事故实证：live 探针曾误绿——DSN 坏时 health 仍可返回部分字段）
        const isMultiuser = inspectEnv.some((e) => e === 'MU_MODE=multiuser');
        // 兼容旧版本 health（无 mu_ready 字段=放行）；显式 false 才等待——
        // rc3+ 探针必输出该字段，掩码 DSN 场景会恒 false→超时回滚（事故防护不放宽）
        if (isMultiuser && /mu_ready=false/.test(out)) {
          log('健康检查: ' + out.replace('HEALTH ', '').trim() + '（live 但 mu_schema_ready 未就绪，等待）');
          continue;
        }
        live = true; log('健康检查通过（data_mode=live）: ' + out.replace('HEALTH ', '').trim()); break;
      }
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
    return rollbackFn('data_mode 未达 live（DB/依赖不可达或启动失败）');
  }

  // ── 后置检查 1b：构建身份核对（发布产物一致性波）——期望 commit 不符=回滚 ──
  if (expectCommit && containerName) {
    const idScript = "fetch('http://127.0.0.1:' + (process.env.CONSOLE_PORT || 4730) + '/api/health')"
      + ".then(r=>r.json()).then(j=>console.log('ID ' + (j.git_commit ?? 'null') + ' ' + (j.frontend_bundle ? j.frontend_bundle.file : 'null')))"
      + ".catch(e=>console.log('ID ERR'))";
    const idr = exec(['exec', containerName, 'node', '-e', idScript]);
    // health.git_commit 报短哈希（8 位，2026-10-15 生产切换实证）——接受为期望值的前缀；
    // 全 40 位形态同样接受（互为前缀）。空/读取失败仍回滚。
    const m = String(idr.stdout || '').match(/ID ([0-9a-f]+|null) (.+)/);
    const got = m ? m[1] : null;
    const expectNorm = String(expectCommit || '').toLowerCase();
    const prefixOk = got && got !== 'null'
      && (expectNorm.startsWith(got) || got.startsWith(expectNorm.slice(0, got.length)));
    if (!prefixOk) {
      return rollbackFn(`构建身份不符：health.git_commit=${got ?? '读取失败'}，期望 ${expectCommit.slice(0, 12)}…——镜像与源码可能不一致`);
    }
    log('构建身份核对通过（git_commit=' + got + '…）');
  }

  // ── 后置检查 2：新容器内执行器凭据校验（与前置同款形状分派探针，防环境注入差异；
  //    失败→回滚到旧镜像。UNCONFIGURED=可选依赖未配置，如实放行不触发回滚）──
  if (containerName && hasAuthEnv && !skipMatrix) {
    const { verdict, detail, probeError } = runAuthProbe(containerName);
    if (probeError || !verdict) {
      return rollbackFn('后置执行器凭据探针未返回（' + (probeError ? String(probeError.message ?? probeError).slice(0, 80) : '无输出') + '）——回滚');
    }
    if (verdict === 'AUTH' || verdict === 'NETWORK' || verdict === 'SERVICE' || verdict === 'EXECUTOR_MISMATCH') {
      return rollbackFn('后置执行器凭据校验失败（' + verdict + ' ' + detail + '）——回滚');
    }
    log('后置执行器凭据校验通过（' + (verdict === 'UNCONFIGURED' ? '未配置如实放行' : detail) + '）');
  }
  log('SWAP OK');
  return 0;
}

module.exports = { FALLBACK_RESTART_POLICY, resolveRestartPolicy, mountSpec, portSpecs, buildDockerRunArgs, safeSwapSummary, swapConsole, validateForDeploy, envValueLooksRedacted, matrixLoginBody, pickRollbackImage, compareWithLive };

if (require.main === module) {
  try { swapConsole(process.argv.slice(2)).then((c) => process.exit(c)).catch((e) => { console.error('swap-console: ' + (e && e.message ? e.message : e)); process.exit(1); }); }
  catch (e) { console.error('swap-console: ' + (e && e.message ? e.message : e)); process.exit(1); }
}
