#!/usr/bin/env node
// recreate-console-verified.mjs — 生产 console（beta-mp-console）受控重建（2026-10-21 matrixpw 事故固化）。
//
// 背景：2026-10-21 生产 console 以不完整 env 重建，MU_AGENTTEAMS_MATRIX_PASSWORD 缺失
// → 执行器门如实拒绝（MT_NOT_CONFIGURED）→ 修复轮被拦。本脚本是**规范重建入口**：
//   ① 锚点↔live 对比（镜像/env 键集不一致即拒绝，防静默回滚 live 漂移）
//   ② 变更前形状断言（必需 env 全非空才放行）
//   ③ 同镜像同参重建（旧容器**改名保留**——失败即改名回切，成功并验证后才清除）
//   ④ 变更后身份验证（实际执行：health 版本烙印比对 + Tuwunel 登录探针；只输出状态码）
//
// 用法：
//   node recreate-console-verified.mjs <anchor-inspect.json>            # dry-run：形状断言+计划
//   node recreate-console-verified.mjs <anchor-inspect.json> --execute  # 执行重建+验证
// 测试模式（隔离验收用，不触生产名/端口）：
//   ... --execute --name itest-console-<hex> --skip-ports
//
// 纪律：任何明文凭据不落日志/报告；密码真值必须来自受控秘密源的锚点文件（找回密码不是恢复流程）。
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';

const ANCHOR = process.argv[2];
const EXECUTE = process.argv.includes('--execute');
const NAME = process.argv.includes('--name')
  ? process.argv[process.argv.indexOf('--name') + 1] : 'beta-mp-console';
const SKIP_PORTS = process.argv.includes('--skip-ports');
const JOIN_NET = process.argv.includes('--join-net') ? process.argv[process.argv.indexOf('--join-net') + 1] : null;
const REQUIRED_ENV = [
  'MU_MODE', 'CONSOLE_PG_DSN', 'CONSOLE_SESSION_SECRET',
  'MU_AGENTTEAMS_BASE_URL', 'MU_AGENTTEAMS_TOKEN',
  'MU_AGENTTEAMS_MATRIX_URL', 'MU_AGENTTEAMS_MATRIX_USER', 'MU_AGENTTEAMS_MATRIX_PASSWORD',
  'MU_GITHUB_OAUTH_CLIENT_ID', 'MU_GITHUB_OAUTH_CLIENT_SECRET',
];
if (!ANCHOR) { console.error('usage: recreate-console-verified.mjs <anchor-inspect.json> [--execute] [--name X] [--skip-ports]'); process.exit(2); }
const load = (p) => { const j = JSON.parse(fs.readFileSync(p, 'utf8')); return Array.isArray(j) ? j[0] : j; };
const toMap = (a) => Object.fromEntries((a ?? []).map((x) => { const i = x.indexOf('='); return [x.slice(0, i), x.slice(i + 1)]; }));
const dk = (cmd, ...a) => execFileSync('docker', [cmd, ...a], { stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const anchor = load(ANCHOR);
const env = toMap(anchor.Config.Env);
console.log('anchor:', ANCHOR, '| target name:', NAME);

// ① 形状断言
const missing = REQUIRED_ENV.filter((k) => !(env[k] && env[k].length > 0));
if (missing.length > 0) {
  console.error('SHAPE_ASSERT_FAILED — 以下必需 env 缺失或为空（拒绝重建，零变更）:');
  for (const k of missing) console.error('  -', k);
  console.error('先从受控秘密源补齐锚点再执行；不得跳过本断言。');
  process.exit(1);
}
console.log('shape assert: PASS（必需 env 全非空）');

// ② 锚点↔live 对比（live 存在时）
const live = (() => { try { return JSON.parse(dk('inspect', NAME)); } catch { return null; } })();
if (Array.isArray(live)) {
  const L = live[0];
  const lenv = toMap(L.Config.Env);
  const imageDiff = L.Image !== anchor.Image;
  const keysDiff = [
    ...Object.keys(env).filter((k) => !(k in lenv)).map((k) => `anchor-only:${k}`),
    ...Object.keys(lenv).filter((k) => !(k in env)).map((k) => `live-only:${k}`),
    ...Object.keys(env).filter((k) => (k in lenv) && (env[k] === '') !== (lenv[k] === '')).map((k) => `emptiness:${k}`),
  ];
  console.log('live-vs-anchor: image_match=' + String(!imageDiff) + ' env_key_diffs=' + keysDiff.length);
  if (keysDiff.length > 0) for (const d of keysDiff) console.log('  -', d);
  if (imageDiff) {
    console.error('REFUSE: live 镜像与锚点不一致——锚点可能过期；先重新 inspect 固化新锚点（--force 不提供，禁止跳过）。');
    process.exit(1);
  }
  if (keysDiff.length > 0) {
    console.error('REFUSE: live env 键集/空值性与锚点不一致（见上）——先对齐锚点；live 值差异不阻断（值不比对，只比对键与空值性）。');
    process.exit(1);
  }
} else {
  console.log('live: 不存在（首次创建或已删除）——跳过对比');
}

if (!EXECUTE) { console.log('dry-run（加 --execute 执行）。'); process.exit(0); }

// ③ 重建（旧容器改名保留=回滚锚；改名后**先断开其非 bridge 网络**释放锚 IP——
// 改名容器仍持有 IP，不等断开会令新容器 --ip 绑定失败；失败即回切（重连+改名还原））
const OLD = `${NAME}-pre-${new Date().toISOString().replace(/[-:.TZ]/g, '').slice(0, 14)}`;
const liveExists = Array.isArray(live);
const extraNets = liveExists ? Object.entries(anchor.NetworkSettings.Networks).filter(([n]) => n !== 'bridge') : [];
if (liveExists) {
  dk('rename', NAME, OLD);
  for (const [net] of extraNets) { try { dk('network', 'disconnect', net, OLD); } catch { /* 尽力释放 */ } }
  console.log('old container renamed（网络已断开释放 IP）→', OLD);
}

const buildArgs = () => {
  const a = ['run', '-d', '--name', NAME, '--restart', anchor.HostConfig.RestartPolicy?.Name ?? 'unless-stopped'];
  if (!SKIP_PORTS) for (const [p, bs] of Object.entries(anchor.HostConfig.PortBindings ?? {}))
    for (const b of bs) a.push('-p', `${b.HostIp}:${b.HostPort}:${p}`);
  for (const b of anchor.HostConfig.Binds ?? []) a.push('-v', b);
  for (const [k, v] of Object.entries(env)) a.push('-e', `${k}=${v}`);
  a.push(anchor.Image);
  return a;
};
const rollback = async () => {
  try { try { dk('rm', '-f', NAME); } catch { /* 新容器可能未建成 */ } 
    if (liveExists) {
      for (const [net, cfg] of extraNets) {
        const aliases = (cfg.Aliases ?? []).filter(Boolean).flatMap((x) => ['--alias', x]);
        const ip = cfg.IPAddress ? ['--ip', cfg.IPAddress] : [];
        try { dk('network', 'connect', ...aliases, ...ip, net, OLD); } catch { /* 尽力回接 */ }
      }
      dk('rename', OLD, NAME);
      console.log('ROLLBACK: 旧容器已回接网络并改名还原 →', NAME);
    }
  } catch { console.error('ROLLBACK FAILED — 手工介入。'); }
};
try {
  const id = dk('run', ...buildArgs().slice(1));
  console.log('recreated:', id.slice(0, 12));
  if (JOIN_NET) { dk('network', 'connect', JOIN_NET, NAME); console.log('test-mode joined net:', JOIN_NET); }
  for (const [net, cfg] of extraNets) {
    const aliases = (cfg.Aliases ?? []).filter(Boolean).flatMap((x) => ['--alias', x]);
    const ip = cfg.IPAddress ? ['--ip', cfg.IPAddress] : [];
    dk('network', 'connect', ...aliases, ...ip, net, NAME);
    console.log('network connected:', net, 'aliases:', (cfg.Aliases ?? []).join(','));
  }
} catch (e) {
  console.error('RECREATE_FAILED:', e.message?.slice(0, 200));
  await rollback();
  process.exit(1);
}

// ④ 变更后身份验证（实执行；任一失败=回滚）
const port = SKIP_PORTS ? null : Object.values(anchor.HostConfig.PortBindings ?? {})[0]?.[0]?.HostPort;
const fail = async (why) => {
  console.error('POST_CHECK FAILED:', why);
  await rollback();
  process.exit(1);
};
let healthy = '';
for (let i = 0; i < 36; i++) { await sleep(5000); try { healthy = dk('inspect', '--format', '{{.State.Health.Status}}', NAME); } catch { /* */ } if (healthy === 'healthy') break; }
if (healthy !== 'healthy') await fail(`health=${healthy || 'unknown'}`);
console.log('health: healthy');
// 版本烙印 + Matrix 登录探针均从容器内执行（atnet 域名宿主不可达；与端口发布无关）
const expectVer = env.MERGEPILOT_VERSION ?? '';
const inCurl = (url) => dk('exec', NAME, 'node', '-e',
  `fetch(${JSON.stringify(url)}).then(r=>r.text()).then(t=>process.stdout.write(t)).catch(e=>{process.stdout.write('ERR '+e.message);process.exit(1)})`);
if (expectVer) {
  const h = JSON.parse(inCurl('http://127.0.0.1:4730/api/health'));
  if (!h.ok || h.version !== expectVer) await fail(`health version mismatch: ${h.version} vs anchor ${expectVer}`);
  console.log('version stamp:', h.version);
}
const mtxUser = env.MU_AGENTTEAMS_MATRIX_USER ?? '';
const mtxBase = (env.MU_AGENTTEAMS_MATRIX_URL ?? '').replace(/\/+$/, '');
const mtxBody = JSON.stringify({ type: 'm.login.password', identifier: { type: 'm.id.user', user: mtxUser }, password: env.MU_AGENTTEAMS_MATRIX_PASSWORD });
const mtxStatus = execFileSync('docker', ['exec', '-e', `MTXB=${mtxBody}`, NAME, 'node', '-e',
  `fetch(${JSON.stringify(mtxBase + '/_matrix/client/v3/login')},{method:'POST',headers:{'content-type':'application/json'},body:process.env.MTXB}).then(r=>{console.log(r.status);}).catch(e=>console.log(0))`],
  { stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
console.log('tuwunel login probe:', mtxStatus, mtxStatus === '200' ? 'OK' : 'FAIL');
if (mtxStatus !== '200') await fail('matrix login probe failed');
if (liveExists) { try { dk('rm', OLD); console.log('old container removed（验证通过）:', OLD); } catch { /* 保留不致命 */ } }
console.log('RECREATE_VERIFIED: 全部通过（形状/对比/重建/健康/版本/身份）');
