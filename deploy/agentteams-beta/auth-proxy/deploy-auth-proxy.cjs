#!/usr/bin/env node
'use strict';
// deploy/agentteams-beta/auth-proxy/deploy-auth-proxy.cjs — AgentTeams 认证代理部署器。
//
// 职责：把 nginx-auth-proxy.conf.template 渲染为含真实凭据的运行时配置（写入
// 调用方指定的受保护目录，永不写入仓库），并以固定参数部署 nginx 代理容器。
//
// 纪律（与 scripts/swap-console.cjs 同一红线）：
//  * fail-closed——入口凭据/上游 token 缺失或过短即拒绝部署；网络不存在即拒绝；
//  * 秘密零回显——摘要只输出 sha256 前 16 位；完整 docker run 参数绝不打印；
//  * 上游 token 生产路径=部署时从 ctrl 容器现读（/var/run/agentteams/cli-token），
//    不接受命令行传值；--upstream-token-file 仅供测试/离线渲染使用；
//  * 幂等——重复部署=替换同名容器（rm -f + run），配置目录内容以最新渲染为准。
//
// 用法：
//   node deploy-auth-proxy.cjs \
//     --entry-credential-file <受保护秘密文件> \
//     [--out-dir <渲染输出目录>] [--ctrl-container agentteams-beta-ctrl] \
//     [--ctrl-host agentteams-beta-ctrl] [--network agentteams-beta_atnet] \
//     [--name at-auth-proxy] [--publish 127.0.0.1:28565:8091 | --no-publish] \
//     [--template <模板路径>] [--upstream-token-file <测试用>]
//
// 测试：renderProxyConfig 为纯函数（console/backend/test/agentteams-auth-proxy-config.test.mjs）；
//       deployProxy/rmProxy 供 docker 门控集成测试复用（test/agentteams-auth-proxy.integration.test.mjs）。

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const DEFAULT_CTRL_HOST = 'agentteams-beta-ctrl';
const DEFAULT_CTRL_CONTAINER = 'agentteams-beta-ctrl';
const DEFAULT_NETWORK = 'agentteams-beta_atnet';
const DEFAULT_NAME = 'at-auth-proxy';
const DEFAULT_PUBLISH = '127.0.0.1:28565:8091';
// 供应链钉扎（RUNBOOK §11 同口径）：部署时记录于 auth-proxy/README「供应链」
const DEFAULT_IMAGE = 'nginx:alpine@sha256:df221db836e1754089190208cee7eeda94f233197056426eda74a43ab1abeac2';
const CTRL_TOKEN_PATH = '/var/run/agentteams/cli-token';
const RENDERED_CONF_NAME = 'default.conf';

function sha16(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 16);
}

/** 纯函数：模板渲染。任一凭据缺失/过短即抛错（fail-closed），残留占位符即抛错。 */
function renderProxyConfig(templateText, { entryCredential, upstreamToken, ctrlHost = DEFAULT_CTRL_HOST } = {}) {
  if (typeof templateText !== 'string' || !templateText.includes('@AT_PROXY_ENTRY_CREDENTIAL@')) {
    throw new Error('render: template missing entry-credential placeholder');
  }
  if (typeof entryCredential !== 'string' || entryCredential.trim().length < 16) {
    throw new Error('render: entryCredential missing or too short');
  }
  if (typeof upstreamToken !== 'string' || upstreamToken.trim().length < 16) {
    throw new Error('render: upstreamToken missing or too short');
  }
  const out = templateText
    .split('@AT_PROXY_ENTRY_CREDENTIAL@').join(entryCredential.trim())
    .split('@AT_PROXY_UPSTREAM_TOKEN@').join(upstreamToken.trim())
    .split('@AT_CTRL_HOST@').join(String(ctrlHost || DEFAULT_CTRL_HOST));
  if (out.includes('@AT_PROXY_')) throw new Error('render: placeholder residue after render');
  return out;
}

function docker(args, { input } = {}) {
  const r = spawnSync('docker', args, { encoding: 'utf8', input, maxBuffer: 64 * 1024 * 1024 });
  if (r.status !== 0) {
    throw new Error(`docker ${args.join(' ').split(' ')[0]}… failed: ${(r.stderr || r.stdout || '').trim().slice(0, 200)}`);
  }
  return (r.stdout || '').trim();
}

function readTrimmed(file, label) {
  if (!file) throw new Error(`${label}: file path required`);
  const v = fs.readFileSync(file, 'utf8').trim();
  if (v.length < 16) throw new Error(`${label}: value too short`);
  return v;
}

/**
 * 部署认证代理（同步）。返回脱敏摘要；任何失败抛错，绝不半宣称成功。
 * opts: templatePath, outDir, entryCredentialFile?, entryCredential?, upstreamTokenFile?,
 *       ctrlContainer?, ctrlHost?, network?, name?, publish?|noPublish?, image?
 */
function deployProxy(opts = {}) {
  const templatePath = opts.templatePath || path.join(__dirname, 'nginx-auth-proxy.conf.template');
  const outDir = opts.outDir || path.join(__dirname, 'rendered');
  const network = opts.network || DEFAULT_NETWORK;
  const name = opts.name || DEFAULT_NAME;
  const ctrlHost = opts.ctrlHost || DEFAULT_CTRL_HOST;
  const image = opts.image || DEFAULT_IMAGE;

  // 1) 入口凭据：文件优先，其次环境变量；绝不入库/入日志
  const entryCredential = opts.entryCredentialFile
    ? readTrimmed(opts.entryCredentialFile, 'entryCredentialFile')
    : String(opts.entryCredential ?? process.env.AT_PROXY_ENTRY_CREDENTIAL ?? '');
  if (entryCredential.trim().length < 16) throw new Error('entry credential: provide --entry-credential-file or AT_PROXY_ENTRY_CREDENTIAL');

  // 2) 上游 token：默认现读 ctrl 容器；仅测试/离线允许文件覆盖
  const upstreamToken = opts.upstreamTokenFile
    ? readTrimmed(opts.upstreamTokenFile, 'upstreamTokenFile')
    : docker(['exec', opts.ctrlContainer || DEFAULT_CTRL_CONTAINER, 'cat', CTRL_TOKEN_PATH]);

  // 3) 渲染并落盘到受保护输出目录（0600；该目录必须在 .gitignore 覆盖范围内）
  const template = fs.readFileSync(templatePath, 'utf8');
  const rendered = renderProxyConfig(template, { entryCredential, upstreamToken, ctrlHost });
  fs.mkdirSync(outDir, { recursive: true });
  const configPath = path.join(outDir, RENDERED_CONF_NAME);
  fs.writeFileSync(configPath, rendered, { mode: 0o600 });
  try { fs.chmodSync(configPath, 0o600); } catch { /* 平台差异不阻断 */ }

  // 4) 网络必须已存在（生产 atnet 由 agentteams 栈创建）——不静默建网
  docker(['network', 'inspect', network]);

  // 5) 替换式部署
  try { docker(['rm', '-f', name]); } catch { /* 首次部署不存在 */ }
  const runArgs = ['run', '-d', '--name', name, '--restart', 'unless-stopped',
    '--network', network, '--network-alias', name];
  if (!opts.noPublish) runArgs.push('-p', opts.publish || DEFAULT_PUBLISH);
  runArgs.push('-v', `${outDir}:/etc/nginx/conf.d:ro`, image);
  docker(runArgs);

  // 6) 解析宿主发布端口（--no-publish 或 ephemeral 端口时从 inspect 读回）
  let hostPort = null;
  if (!opts.noPublish) {
    const want = (opts.publish || DEFAULT_PUBLISH).split(':')[1];
    hostPort = want || null;
    if (!hostPort) {
      const inspect = JSON.parse(docker(['inspect', name]));
      hostPort = inspect?.[0]?.NetworkSettings?.Ports?.['8091/tcp']?.[0]?.HostPort ?? null;
    }
  }

  return {
    name, network, ctrlHost, configPath, hostPort,
    publish: opts.noPublish ? null : (opts.publish || DEFAULT_PUBLISH),
    entrySha: sha16(entryCredential), upstreamSha: sha16(upstreamToken), image,
  };
}

/** 清理（测试/卸载用）：仅删容器，不动网络与卷。 */
function rmProxy(name) {
  try { docker(['rm', '-f', name]); return true; } catch { return false; }
}

function readFlag(argv, flag) {
  const i = argv.indexOf(flag);
  return i >= 0 ? argv[i + 1] : undefined;
}

function main(argv = process.argv.slice(2)) {
  if (argv.includes('--help') || argv.includes('-h')) {
    console.log(fs.readFileSync(__filename, 'utf8').split('\n').filter((l) => l.startsWith('//')).map((l) => l.replace(/^\/\/ ?/, '')).join('\n'));
    return 0;
  }
  const summary = deployProxy({
    templatePath: readFlag(argv, '--template'),
    outDir: readFlag(argv, '--out-dir'),
    entryCredentialFile: readFlag(argv, '--entry-credential-file'),
    upstreamTokenFile: readFlag(argv, '--upstream-token-file'),
    ctrlContainer: readFlag(argv, '--ctrl-container'),
    ctrlHost: readFlag(argv, '--ctrl-host'),
    network: readFlag(argv, '--network'),
    name: readFlag(argv, '--name'),
    publish: readFlag(argv, '--publish'),
    noPublish: argv.includes('--no-publish'),
    image: readFlag(argv, '--image'),
  });
  console.log(`deployed: container=${summary.name} network=${summary.network} ctrl=${summary.ctrlHost}`);
  console.log(`publish=${summary.publish ?? 'none'} hostPort=${summary.hostPort ?? 'n/a'}`);
  console.log(`entry-credential sha256:${summary.entrySha}… upstream-token sha256:${summary.upstreamSha}…（值不回显）`);
  console.log(`config=${summary.configPath}（含真实凭据——确认位于受保护目录，勿入库）`);
  return 0;
}

if (require.main === module) process.exit(main());

module.exports = { renderProxyConfig, deployProxy, rmProxy, DEFAULT_IMAGE, DEFAULT_PUBLISH };
