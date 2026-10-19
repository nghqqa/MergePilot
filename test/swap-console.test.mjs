// test/swap-console.test.mjs — scripts/swap-console.cjs 单元回归（rc.12 PR-C）。
//
// 缺陷背景（rc.11 生产滚动实录）：运维侧 swap2.cjs 硬编码 `--restart no` 冲掉已批准的
// unless-stopped（daemon 重启不自愈）；挂载靠调用方手工传参，首次滚动漏传
// rag-models/rag-corpus 两挂载。本测试锁定：从 inspect JSON 完整继承
// （policy/mounts/ports/net/env），policy=no 一律归一为 unless-stopped。
//
// 零 Docker 依赖：buildDockerRunArgs 为纯函数（返回参数数组、无 IO），直接断言；
// 主入口经 deps.exec mock 注入（真实路径 spawnSync('docker', …) 不被触达）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const {
  FALLBACK_RESTART_POLICY,
  resolveRestartPolicy,
  buildDockerRunArgs,
  swapConsole,
} = require('../scripts/swap-console.cjs');

// flag 的相邻取值（如 --restart unless-stopped）
const flagValue = (args, flag) => {
  const i = args.indexOf(flag);
  return i === -1 ? undefined : args[i + 1];
};
// 相邻对在位（'-p', '127.0.0.1:48500:4730'；重复 flag 扫全部位置）
const pairIn = (args, a, b) => {
  for (let i = 0; i < args.length - 1; i++) {
    if (args[i] === a && args[i + 1] === b) return true;
  }
  return false;
};
const valuesOf = (args, flag) => {
  const out = [];
  for (let i = 0; i < args.length; i++) if (args[i] === flag) out.push(args[i + 1]);
  return out;
};

const baseInspect = (over = {}) => ({
  Name: '/mp-console',
  HostConfig: { RestartPolicy: { Name: 'no' }, ...over.HostConfig },
  Config: { Env: [], ...over.Config },
  Mounts: [],
  ...over,
});

// ---- restart policy（rc.11 缺陷回归锁）----

test('policy=no → --restart unless-stopped（硬编码 no 禁止回归）', () => {
  const args = buildDockerRunArgs(baseInspect(), 'mp:rc12');
  assert.equal(flagValue(args, '--restart'), 'unless-stopped');
  assert.equal(resolveRestartPolicy(baseInspect()), 'unless-stopped');
});

test('policy 缺失/空串 → 归一 unless-stopped', () => {
  const noField = { Name: '/x', HostConfig: {}, Config: {}, Mounts: [] };
  assert.equal(resolveRestartPolicy(noField), FALLBACK_RESTART_POLICY);
  assert.equal(resolveRestartPolicy(baseInspect({ HostConfig: { RestartPolicy: { Name: '' } } })), FALLBACK_RESTART_POLICY);
  assert.equal(flagValue(buildDockerRunArgs(noField, 'img'), '--restart'), 'unless-stopped');
});

test('policy=always / unless-stopped → 原样保留', () => {
  assert.equal(resolveRestartPolicy(baseInspect({ HostConfig: { RestartPolicy: { Name: 'always' } } })), 'always');
  assert.equal(
    flagValue(buildDockerRunArgs(baseInspect({ HostConfig: { RestartPolicy: { Name: 'always' } } }), 'img'), '--restart'),
    'always',
  );
  assert.equal(
    flagValue(buildDockerRunArgs(baseInspect({ HostConfig: { RestartPolicy: { Name: 'unless-stopped' } } }), 'img'), '--restart'),
    'unless-stopped',
  );
});

test('restart policy 不进 -e 域（由 --restart 显式传递）', () => {
  const args = buildDockerRunArgs(baseInspect(), 'img');
  assert.ok(!valuesOf(args, '-e').some((v) => v.startsWith('restart') || v.includes('unless-stopped')));
});

// ---- Mounts 全量继承（rc.11 挂载丢失回归锁）----

test('Mounts 两条（含 :ro）→ 两条 -v 逐条在位且顺序/模式一致', () => {
  const inspect = baseInspect({
    Mounts: [
      { Type: 'bind', Source: '/srv/mp/rag-models', Destination: '/models', Mode: 'ro', RW: false },
      { Type: 'bind', Source: '/srv/mp/rag-corpus', Destination: '/corpus', Mode: '', RW: true },
    ],
  });
  const args = buildDockerRunArgs(inspect, 'mp:rc12');
  const vs = valuesOf(args, '-v');
  assert.deepEqual(vs, ['/srv/mp/rag-models:/models:ro', '/srv/mp/rag-corpus:/corpus']);
});

test('Mode 空但 RW=false（compose 只读常见形态）→ 补 :ro；具名卷 Source 空用卷名', () => {
  const inspect = baseInspect({
    Mounts: [
      { Type: 'bind', Source: '/etc/ssl', Destination: '/etc/ssl', Mode: '', RW: false },
      { Type: 'volume', Source: '', Name: 'mp-data', Destination: '/data', Mode: '', RW: true },
    ],
  });
  const args = buildDockerRunArgs(inspect, 'img');
  const vs = valuesOf(args, '-v');
  assert.deepEqual(vs, ['/etc/ssl:/etc/ssl:ro', 'mp-data:/data']);
});

test('extraMounts 追加在继承项之后，继承项不丢失', () => {
  const inspect = baseInspect({
    Mounts: [{ Type: 'bind', Source: '/srv/mp/rag-models', Destination: '/models', Mode: 'ro', RW: false }],
  });
  const args = buildDockerRunArgs(inspect, 'img', { extraMounts: ['/tmp/extra:/extra'] });
  const vs = valuesOf(args, '-v');
  assert.deepEqual(vs, ['/srv/mp/rag-models:/models:ro', '/tmp/extra:/extra']);
});

// ---- 端口 / 网络 ----

test('PortBindings {4730: 127.0.0.1:48500} → -p 127.0.0.1:48500:4730 在位', () => {
  const inspect = baseInspect({
    HostConfig: { RestartPolicy: { Name: 'no' }, PortBindings: { '4730/tcp': [{ HostIp: '127.0.0.1', HostPort: '48500' }] } },
  });
  const args = buildDockerRunArgs(inspect, 'img');
  assert.ok(pairIn(args, '-p', '127.0.0.1:48500:4730'));
});

test('多端口逐条映射；udp 带协议后缀；无 HostIp 不加前缀', () => {
  const inspect = baseInspect({
    HostConfig: {
      RestartPolicy: { Name: 'no' },
      PortBindings: {
        '8080/tcp': [{ HostIp: '', HostPort: '8080' }],
        '53/udp': [{ HostIp: '0.0.0.0', HostPort: '1053' }],
      },
    },
  });
  const args = buildDockerRunArgs(inspect, 'img');
  const ps = valuesOf(args, '-p');
  assert.deepEqual(ps.sort(), ['0.0.0.0:1053:53/udp', '8080:8080']);
});

test('NetworkMode=agentteams-beta_atnet → --network 在位；default 不输出 --network', () => {
  const inspect = baseInspect({ HostConfig: { RestartPolicy: { Name: 'no' }, NetworkMode: 'agentteams-beta_atnet' } });
  const args = buildDockerRunArgs(inspect, 'img');
  assert.ok(pairIn(args, '--network', 'agentteams-beta_atnet'));

  const plain = buildDockerRunArgs(baseInspect({ HostConfig: { RestartPolicy: { Name: 'no' }, NetworkMode: 'default' } }), 'img');
  assert.ok(!plain.includes('--network'));
});

// ---- Env 继承与追加 ----

test('Env 38 条 → 38 个 -e 逐条在位', () => {
  const env = Array.from({ length: 38 }, (_, i) => `K${i}=v${i}`);
  const inspect = baseInspect({ Config: { Env: env } });
  const args = buildDockerRunArgs(inspect, 'img');
  const es = valuesOf(args, '-e');
  assert.equal(es.length, 38);
  assert.ok(pairIn(args, '-e', 'K0=v0'));
  assert.ok(pairIn(args, '-e', 'K37=v37'));
});

test('extraEnv 追加在位；版本烙印过滤（版本只随镜像走）', () => {
  const inspect = baseInspect({ Config: { Env: ['MERGEPILOT_VERSION=rc.11', 'EXISTING=keep'] } });
  const args = buildDockerRunArgs(inspect, 'img', { extraEnv: ['MERGEPILOT_VERSION=rc.12', 'NEWKEY=new'] });
  const es = valuesOf(args, '-e');
  assert.ok(!es.includes('MERGEPILOT_VERSION=rc.11'), 'running 烙印被过滤（否则覆盖新镜像版本）');
  assert.ok(es.includes('MERGEPILOT_VERSION=rc.12'), 'extraEnv 显式版本合法');
  assert.ok(es.includes('NEWKEY=new')); // 新键追加
  assert.ok(es.includes('EXISTING=keep'));
  // 无 extraEnv 版本 → 镜像烘焙值生效（env 中无该键）
  const args2 = buildDockerRunArgs(inspect, 'img');
  assert.ok(!valuesOf(args2, '-e').some((e) => e.startsWith('MERGEPILOT_VERSION=')));
});

// ---- 参数形态 ----

test('image 居末；容器名继承 inspect.Name（去前导 /）', () => {
  const args = buildDockerRunArgs(baseInspect(), 'mp-console:rc12');
  assert.equal(args[args.length - 1], 'mp-console:rc12');
  assert.ok(pairIn(args, '--name', 'mp-console'));
});

test('rc.11 生产实录综合回归：policy=no + rag 双挂载 + 端口 + 网络 + 38 env 一次完整继承', () => {
  const env = Array.from({ length: 38 }, (_, i) => `MP_ENV_${i}=${i}`);
  const inspect = {
    Name: '/mergepilot-console',
    HostConfig: {
      RestartPolicy: { Name: 'no' },                 // rc.11 滚动后被冲掉的生产现场
      NetworkMode: 'agentteams-beta_atnet',
      PortBindings: { '4730/tcp': [{ HostIp: '127.0.0.1', HostPort: '48500' }] },
    },
    Config: { Env: env },
    Mounts: [
      { Type: 'bind', Source: '/srv/mp/rag-models', Destination: '/models', Mode: 'ro', RW: false },
      { Type: 'bind', Source: '/srv/mp/rag-corpus', Destination: '/corpus', Mode: 'ro', RW: false },
    ],
  };
  const args = buildDockerRunArgs(inspect, 'mp-console:rc12');
  assert.equal(flagValue(args, '--restart'), 'unless-stopped'); // 缺陷核心：不再回归 no
  assert.deepEqual(valuesOf(args, '-v'), ['/srv/mp/rag-models:/models:ro', '/srv/mp/rag-corpus:/corpus:ro']);
  assert.ok(pairIn(args, '-p', '127.0.0.1:48500:4730'));
  assert.ok(pairIn(args, '--network', 'agentteams-beta_atnet'));
  assert.equal(valuesOf(args, '-e').length, 38);
});

// ---- 主入口（deps.exec mock 注入，零 Docker）----

const writeInspect = (inspect) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-console-test-'));
  const file = path.join(dir, 'inspect.json');
  fs.writeFileSync(file, JSON.stringify(inspect));
  return { dir, file };
};

// ---- rc.26 收口新增：网络继承 + Matrix 前置校验 + live 回滚（2026-10-13）----

test('SW 前置：Matrix 凭据登录非 200 → 切换中止（exit 3），旧容器未被移除', async () => {
  const { dir, file } = writeInspect({
    Name: '/mp-console',
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Env: ['MU_AGENTTEAMS_MATRIX_URL=http://mx.test', 'MU_AGENTTEAMS_MATRIX_USER=u', 'MU_AGENTTEAMS_MATRIX_PASSWORD=p-stale'] },
    Mounts: [],
  });
  try {
    const calls = [];
    const code = await swapConsole([file, 'img'], {
      exec: (args) => {
        calls.push(args);
        if (args[0] === 'exec') return { status: 0, stdout: 'STATUS 403', stderr: '' };
        return { status: 0, stdout: 'x', stderr: '' };
      },
      log: () => {},
    });
    assert.equal(code, 3);
    assert.ok(calls.some((a) => a[0] === 'exec'), '前置登录校验已执行');
    assert.ok(!calls.some((a) => a[0] === 'rm' && a[1] === '-f' && a[2] === 'mp-console'), '旧容器未被移除');
    assert.ok(!calls.some((a) => a[0] === 'run'), '新容器未创建');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('SW 网络继承：inspect 的非默认网络在创建后逐个 connect', async () => {
  const { dir, file } = writeInspect(baseInspect({
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Env: ['A=1'] },
    NetworkSettings: { Networks: { bridge: {}, 'agentteams-beta_atnet': {} } },
  }));
  try {
    const calls = [];
    const code = await swapConsole([file, 'img'], {
      exec: (args) => { calls.push(args); return { status: 0, stdout: 'HEALTH live v', stderr: '' }; },
      log: () => {},
    });
    assert.equal(code, 0);
    const conn = calls.find((a) => a[0] === 'network' && a[1] === 'connect');
    assert.ok(conn, 'network connect 已调用');
    assert.equal(conn[2], 'agentteams-beta_atnet');
    assert.equal(conn[3], 'mp-console');
    assert.ok(!calls.some((a) => a[0] === 'network' && a.includes('bridge')), 'bridge 不重复 connect');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('SW 后置：PG 部署 data_mode 未达 live → 回滚旧镜像（exit 1）', async () => {
  const { dir, file } = writeInspect(baseInspect({
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:rc25', Env: ['CONSOLE_PG_DSN=postgres://pg/mu'] },
  }));
  try {
    const calls = [];
    let runCount = 0;
    const code = await swapConsole([file, 'mp-console:rc26'], {
      exec: (args) => {
        calls.push(args);
        if (args[0] === 'run') { runCount += 1; return { status: 0, stdout: 'new-id\n', stderr: '' }; }
        if (args[0] === 'exec') return { status: 0, stdout: 'HEALTH snapshot v0.2.0-beta.6-rc.26', stderr: '' };
        if (args[0] === 'image' && args[1] === 'inspect') return { status: 0, stdout: 'sha256:deadbeef\n', stderr: '' };
        if (args[0] === 'inspect' && args[1] === '--format') return { status: 1, stdout: '', stderr: 'no such container' };
        return { status: 0, stdout: '', stderr: '' };
      },
      log: () => {},
    });
    assert.equal(code, 1);
    assert.equal(runCount, 2, '旧镜像已按原配置重建（回滚）');
    const rollbackRun = calls.filter((a) => a[0] === 'run')[1];
    assert.ok(rollbackRun.includes('mp-console:rc25'), '回滚 run 使用旧镜像');
    const rmNew = calls.find((a) => a[0] === 'rm' && a[1] === '-f');
    assert.ok(rmNew, '新容器已清理');
    assert.ok(calls.some((a) => a[0] === 'image' && a[1] === 'inspect'), '回滚镜像可用性已预检');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('SW 前置：Matrix 凭据登录非 200 → 切换中止（exit 3），旧容器未被移除', async () => {
  const { dir, file } = writeInspect({
    Name: '/mp-console',
    HostConfig: { RestartPolicy: { Name: 'no' } },
    Config: { Env: ['A=1'] },
    Mounts: [{ Type: 'bind', Source: '/srv/mp/rag-models', Destination: '/models', Mode: 'ro', RW: false }],
  });
  try {
    const calls = [];
    const logs = [];
    const fileInspectRaw = JSON.stringify({ Name: '/mp-console',
      HostConfig: { RestartPolicy: { Name: 'no' } },
      Config: { Env: ['A=1'] },
      Mounts: [{ Type: 'bind', Source: '/srv/mp/rag-models', Destination: '/models', Mode: 'ro', RW: false }] });
    const code = await swapConsole([file, 'mp-console:rc12'], {
      exec: (args) => {
        calls.push(args);
        if (args[0] === 'inspect' && args[1] === 'mp-console') return { status: 0, stdout: fileInspectRaw, stderr: '' };
        if (args[0] === 'image' && args[1] === 'inspect') return { status: 0, stdout: 'deadbeef\n', stderr: '' };
        return { status: 0, stdout: 'deadbeef\n', stderr: '' };
      },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 0);
    const rmCall = calls.find((a) => a[0] === 'rm' && a[1] === '-f');
    const runCall = calls.find((a) => a[0] === 'run');
    assert.ok(rmCall, '旧容器被移除');
    assert.ok(runCall, '新容器被创建');
    assert.equal(runCall[1], '-d');
    assert.ok(runCall.includes('mp-console:rc12'));
    assert.equal(flagValue(runCall, '--restart'), 'unless-stopped');
    assert.ok(pairIn(runCall, '-v', '/srv/mp/rag-models:/models:ro'));
    assert.ok(logs.some((m) => m.includes('docker run -d')));
    assert.ok(logs.some((m) => m.includes('比对通过')), 'live 比对通过上屏');
    assert.ok(logs.some((m) => m.includes('回滚镜像可用')), '回滚镜像可用性预检上屏');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('主入口：--dry-run 只打印命令，不调用 docker', async () => {
  const { dir, file } = writeInspect(baseInspect());
  try {
    let execCalls = 0;
    const code = await swapConsole([file, 'img', '--dry-run'], {
      exec: () => { execCalls++; return { status: 0, stdout: '', stderr: '' }; },
      log: () => {},
    });
    assert.equal(code, 0);
    assert.equal(execCalls, 0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('主入口：docker run 非零退出 → 透传退出码与 stderr', async () => {
  const { dir, file } = writeInspect(baseInspect());
  try {
    let runCount = 0;
    const logs = [];
    const code = await swapConsole([file, 'img'], {
      exec: (args) => {
        if (args[0] === 'run') { runCount += 1; return { status: 125, stdout: '', stderr: 'docker: conflict\n' }; }
        if (args[0] === 'image' && args[1] === 'inspect') return { status: 0, stdout: 'deadbeef\n', stderr: '' };
        if (args[0] === 'inspect' && args[1] === '--format') return { status: 1, stdout: '', stderr: 'no such container' };
        return { status: 0, stdout: '', stderr: '' };
      },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 1, 'run 失败后已尝试回滚；回滚亦失败→🛑 指引退出码 1');
    assert.equal(runCount, 2, 'docker run 恰两次（新容器+回滚容器）');
    assert.ok(logs.some((m) => m.includes('🛑')), '回滚失败 LOUD 指引上屏');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('主入口：缺参 → 用法退出码 2；坏 JSON → 抛错不执行 docker', async () => {
  let execCalls = 0;
  const deps = { exec: () => { execCalls++; return { status: 0, stdout: '', stderr: '' }; }, log: () => {} };
  assert.equal(await swapConsole([], deps), 2);
  assert.equal(await swapConsole(['only-inspect.json'], deps), 2);
  assert.equal(execCalls, 0);

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-console-test-'));
  const bad = path.join(dir, 'bad.json');
  fs.writeFileSync(bad, '{not json');
  try {
    await assert.rejects(() => swapConsole([bad, 'img'], deps), /inspect JSON 解析失败/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  assert.equal(execCalls, 0);
});

test('主入口：inspect 数组形态（docker inspect 默认输出）→ 取首元素', async () => {
  const { dir, file } = writeInspect([baseInspect({ HostConfig: { RestartPolicy: { Name: 'always' } } })]);
  try {
    let runArgs = null;
    const code = await swapConsole([file, 'img'], {
      exec: (args) => { if (args[0] === 'run') runArgs = args; return { status: 0, stdout: 'id\n', stderr: '' }; },
      log: () => {},
    });
    assert.equal(code, 0);
    assert.equal(flagValue(runArgs, '--restart'), 'always');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ── 泄露回归锁（2026-10-08）：inspect 继承 env 含生产秘密——任何输出路径不得回显 ──
// 虚构凭据覆盖七类：session 密钥 / GitHub App 私钥 / LLM key / PG 连接串 /
// webhook secret / OAuth client secret / AgentTeams token+Matrix 密码。
// 全部为假名值；断言 log 捕获中零出现。
const SECRET_FIXTURE = {
  Name: '/mp-console',
  HostConfig: {
    RestartPolicy: { Name: 'unless-stopped' },
    NetworkMode: 'agentteams-beta_atnet',
    PortBindings: { '4730/tcp': [{ HostIp: '127.0.0.1', HostPort: '48500' }] },
  },
  Config: {
    Env: [
      'CONSOLE_SESSION_SECRET=dummy-session-secret-ABCDEF',
      'MU_GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----DUMMYKEY123-----END RSA PRIVATE KEY-----',
      'MU_LLM_API_KEY=dummy-llm-key-XYZ789',
      'CONSOLE_PG_DSN=postgres://pguser:dummy-pg-pass@pg:5432/mu',
      'MU_GITHUB_WEBHOOK_SECRET=dummy-webhook-secret-42',
      'MU_GITHUB_OAUTH_CLIENT_SECRET=dummy-oauth-secret-77',
      'MU_AGENTTEAMS_TOKEN=dummy-agentteams-jwt-token',
      'MU_AGENTTEAMS_MATRIX_PASSWORD=dummy-matrix-pass-13',
      'NODE_ENV=production',
    ],
  },
  Mounts: [{ Type: 'bind', Source: 'D:/demo/prod-data', Destination: '/app/data', Mode: '' }],
};
const SECRET_VALUES = SECRET_FIXTURE.Config.Env.map((e) => e.split('=').slice(1).join('='));
const assertNoSecrets = (out) => {
  for (const v of SECRET_VALUES) {
    if (v && v.length > 8) assert.ok(!out.includes(v), `秘密值不得出现在输出（前 8 位=${v.slice(0, 8)}…）`);
  }
  assert.ok(!out.includes('-e CONSOLE_SESSION_SECRET='), '完整参数（含 -e 键=值）不得回显');
  assert.ok(!out.includes('docker run -d --name'), '完整 docker run 命令行不得回显');
};

test('泄露回归：dry-run 输出安全摘要（env 键名+数量），零秘密值、零完整参数', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-leak-dry-'));
  const file = path.join(dir, 'inspect.json');
  fs.writeFileSync(file, JSON.stringify(SECRET_FIXTURE));
  try {
    const logs = [];
    const code = await swapConsole([file, 'ghcr.io/nghqqa/mergepilot-console@sha256:' + 'f'.repeat(64), '--dry-run'], {
      exec: () => { throw new Error('dry-run 不得调用 docker'); },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 0);
    const out = logs.join('\n');
    assertNoSecrets(out);
    assert.ok(out.includes('[DRY-RUN]'), 'dry-run 标记在');
    assert.ok(out.includes('env=9 项（仅键名，值不回显）'), 'env 仅数量+键名');
    assert.ok(out.includes('CONSOLE_SESSION_SECRET'), 'env 键名可回显（值不回显）');
    assert.ok(out.includes('image=ghcr.io/nghqqa/mergepilot-console@sha256:'), '镜像引用在');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('泄露回归：正式执行成功路径——输出容器标记与安全摘要，零秘密值', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-leak-run-'));
  const file = path.join(dir, 'inspect.json');
  fs.writeFileSync(file, JSON.stringify(SECRET_FIXTURE));
  try {
    let runArgs = null;
    const logs = [];
    const code = await swapConsole([file, 'ghcr.io/nghqqa/mergepilot-console@sha256:' + 'f'.repeat(64)], {
      exec: (args) => {
        if (args[0] === 'run') runArgs = args;
        if (args[0] === 'exec') return { status: 0, stdout: String(args.at(-1)).includes('health') ? 'HEALTH live v0.2.0-beta.6-rc.26' : 'STATUS 200', stderr: '' };
        return { status: 0, stdout: 'abc123', stderr: '' };
      },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 0);
    assert.ok(runArgs, 'docker run 已调用');
    const out = logs.join('\n');
    assertNoSecrets(out);
    assert.ok(out.includes('container=abc123'), '容器 ID 标记输出');
    // run 参数本身仍完整传给 docker（继承语义不变）：9 个 -e 秘密在 args 中而非输出中
    assert.equal(valuesOf(runArgs, '-e').length, SECRET_FIXTURE.Config.Env.length, '-e 继承语义不变');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('泄露回归：失败路径——对抗性 stderr（含秘密/连接串/多行私钥）不透传，只报退出码', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-leak-fail-'));
  const file = path.join(dir, 'inspect.json');
  fs.writeFileSync(file, JSON.stringify(SECRET_FIXTURE));
  try {
    const logs = [];
    const hostileStderr = [
      'docker: Error response from daemon: conflicting options.',
      'context: postgres://pguser:dummy-pg-pass@pg:5432/mu',
      '-----BEGIN RSA PRIVATE KEY----- dummy-fixture',
      'DUMMYKEY123-dummy-body',
      '-----END RSA PRIVATE KEY----- dummy-fixture',
    ].join('\n');
    let hostileSeen = 0;
    const code = await swapConsole([file, 'ghcr.io/nghqqa/mergepilot-console@sha256:' + 'f'.repeat(64)], {
      exec: (args) => {
        if (args[0] === 'rm') return { status: 0, stdout: '', stderr: '' };
        if (args[0] === 'image' && args[1] === 'inspect') return { status: 0, stdout: 'deadbeef\n', stderr: '' };
        if (args[0] === 'inspect' && args[1] === '--format') return { status: 1, stdout: '', stderr: 'no such container' };
        if (args[0] === 'run') { hostileSeen += 1; return { status: 125, stdout: '', stderr: hostileStderr }; }
        return { status: 0, stdout: '', stderr: '' };
      },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 1, '新容器与回滚容器 run 均失败→🛑 指引退出码 1');
    assert.equal(hostileSeen, 2, '恰好两次 run（新容器+回滚兜底）');
    const out = logs.join('\n');
    assertNoSecrets(out);
    assert.ok(!out.includes('Error response from daemon'), '对抗性 stderr 原文不透传');
    assert.ok(!out.includes('conflicting options'), 'daemon 消息正文也不透传');
    assert.ok(!out.includes('dummy-pg-pass'), '连接串不泄露');
    assert.ok(!out.includes('DUMMYKEY123'), '私钥体不泄露');
    assert.ok(!out.includes('MU_LLM_API_KEY='), '失败路径也不拼接 args');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('泄露回归：spawn 异常对象单行化（不携带多行负载/秘密）', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-leak-err-'));
  const file = path.join(dir, 'inspect.json');
  fs.writeFileSync(file, JSON.stringify(SECRET_FIXTURE));
  try {
    let caught = null;
    swapConsole([file, 'img:tag'], {
      exec: () => ({ error: Object.assign(new Error('spawn docker ENOENT\nsecret=dummy-session-secret-ABCDEF'), { errno: -4058 }) }),
      log: () => {},
    });
    void caught;
  } catch (e) {
    assert.ok(!String(e.message).includes('\n'), '异常消息单行化');
    assert.ok(!String(e.message).includes('dummy-session-secret-ABCDEF'), '异常消息不含秘密');
    assert.ok(String(e.message).includes('docker run 无法启动'), '异常前缀可控');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('安全摘要：镜像 userinfo（user:pass@registry）脱敏', () => {
  const { safeSwapSummary } = require('../scripts/swap-console.cjs');
  const out = safeSwapSummary(SECRET_FIXTURE, 'opaque-token@ghcr.io/nghqqa/mergepilot-console:tag', { name: 'mp', dryRun: true });
  assert.ok(!out.includes('opaque-token@'), 'userinfo 不回显');
  assert.ok(out.includes('image=ghcr.io/nghqqa/mergepilot-console:tag'), 'host/path 保留');
});

test('泄露回归：safeSwapSummary 纯函数——键名排序在、值不在', () => {
  const { safeSwapSummary } = require('../scripts/swap-console.cjs');
  const out = safeSwapSummary(SECRET_FIXTURE, 'img:tag', { name: 'mp-console', dryRun: true });
  assertNoSecrets(out);
  assert.ok(out.includes('MU_LLM_API_KEY'), '键名回显');
  assert.ok(!out.includes('dummy-llm-key-XYZ789'), '值不回显');
});

// ── rc3 换版事故回归锁（2026-10-18）────────────────────────────────────────
const {
  validateForDeploy,
  envValueLooksRedacted,
  matrixLoginBody,
  pickRollbackImage,
} = require('../scripts/swap-console.cjs');

test('事故回归：掩码 DSN（***）→ validateForDeploy 拒绝，原因点名键位', () => {
  const r = validateForDeploy({ Config: { Env: [
    'MU_MODE=multiuser',
    'CONSOLE_PG_DSN=postgres://postgres:***@beta-mp-pg:5432/mu',
    'CONSOLE_SESSION_SECRET=fake-s-secret-123',
  ] } });
  assert.equal(r.ok, false);
  assert.ok(r.reasons.some((x) => x.includes('CONSOLE_PG_DSN')), '点名 DSN');
  assert.ok(r.reasons.some((x) => x.includes('脱敏')), '点名脱敏副本');
});

test('事故回归：REDACTED 拒绝；空值秘密键降为警告（合法留空不误伤）', () => {
  const r = validateForDeploy({ Config: { Env: [
    'CONSOLE_PG_DSN=postgres://postgres:fake-redacted-pw@db:5432/mu',
    'MU_LLM_API_KEY=REDACTED',
    'MU_GITEE_PAT=',
  ] } });
  assert.equal(r.ok, false, 'REDACTED=展示副本特征，仍拒绝');
  assert.ok(r.reasons.some((x) => x.includes('MU_LLM_API_KEY')));
  assert.ok(!r.reasons.some((x) => x.includes('MU_GITEE_PAT')), '空值不在拒绝理由');
  assert.ok((r.warnings ?? []).some((x) => x.includes('MU_GITEE_PAT')), '空值进警告');
});

test('正常配置（秘密齐备非脱敏）→ 通过；无秘密键的值不受误伤', () => {
  const ok = validateForDeploy({ Config: { Env: [
    'MU_MODE=multiuser',
    'CONSOLE_PG_DSN=postgres://postgres:fake-pw-16chars@db:5432/mu',
    'CONSOLE_SESSION_SECRET=fake-s-secret-123',
    'NODE_ENV=production',
    'PATH=/usr/bin:/bin',
  ] } });
  assert.equal(ok.ok, true, ok.reasons.join('; '));
});

test('MU_MODE=multiuser 缺 CONSOLE_PG_DSN → 拒绝；非 multiuser 不要求', () => {
  const r = validateForDeploy({ Config: { Env: ['MU_MODE=multiuser', 'NODE_ENV=production'] } });
  assert.equal(r.ok, false);
  assert.ok(r.reasons.some((x) => x.includes('CONSOLE_PG_DSN')));
  const ok2 = validateForDeploy({ Config: { Env: ['NODE_ENV=production'] } });
  assert.equal(ok2.ok, true, ok2.reasons.join('; '));
});

test('非秘密键含星号/空值不误伤（如 PATH/提示文案）', () => {
  assert.equal(envValueLooksRedacted('PATH', '/usr/**/bin'), false);
  assert.equal(envValueLooksRedacted('MU_NOTE', ''), false);
});

test('Matrix 登录体：identifier 格式（Tuwunel 旧式顶层 user 字段 403 实证回归锁）', () => {
  const body = JSON.parse(matrixLoginBody('agentteams-beta-admin', 'fake-pw'));
  assert.equal(body.type, 'm.login.password');
  assert.equal(body.identifier.type, 'm.id.user');
  assert.equal(body.identifier.user, 'agentteams-beta-admin');
  assert.equal(body.password, 'fake-pw');
  assert.equal(body.user, undefined, '顶层 user 字段必须移除');
});

test('回滚镜像：优先不可变 Image ID（Config.Image 旧 tag 兜底）', () => {
  const inspect = { Image: 'sha256:' + 'a'.repeat(64), Config: { Image: 'ghcr.io/nghqqa/mergepilot-console:v0.2.0-OLD-TAG' } };
  assert.equal(pickRollbackImage(inspect, 'img:fallback'), 'sha256:' + 'a'.repeat(64));
  assert.equal(pickRollbackImage({ Config: { Image: 'img:tag' } }, 'img:fallback'), 'img:tag');
  assert.equal(pickRollbackImage(null, 'img:fallback'), 'img:fallback');
});


test('事故回归：陈旧 inspect（镜像身份与 live 不一致）→ 停止旧容器前拒绝', async () => {
  const liveRaw = JSON.stringify({ Name: '/mp-console',
    Image: 'sha256:' + 'a'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:rc25', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  const { dir, file } = writeInspect({ Name: '/mp-console',
    Image: 'sha256:' + 'b'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:rc-OLD', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  try {
    const calls = [];
    const logs = [];
    const code = await swapConsole([file, 'mp-console:rc26'], {
      exec: (args) => { calls.push(args); return { status: 0, stdout: liveRaw, stderr: '' }; },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 5, '陈旧 inspect → exit 5');
    assert.ok(logs.some((m) => m.includes('镜像身份不一致')), '身份不一致原因上屏');
    assert.ok(!calls.some((a) => a[0] === 'rm'), '旧容器未被移除');
    assert.ok(!calls.some((a) => a[0] === 'run'), '新容器未被创建');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('事故回归：env 键集缺失（陈旧部分丢失）→ 拒绝并点名数量', async () => {
  const liveRaw = JSON.stringify({ Name: '/mp-console',
    Image: 'sha256:' + 'a'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:rc25', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser', 'MU_LLM_API_KEY=k', 'MU_GITHUB_APP_ID=1'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  const { dir, file } = writeInspect({ Name: '/mp-console',
    Image: 'sha256:' + 'a'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:rc25', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  try {
    const logs = [];
    const calls = [];
    const code = await swapConsole([file, 'mp-console:rc26'], {
      exec: (args) => { calls.push(args); return { status: 0, stdout: liveRaw, stderr: '' }; },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 5);
    assert.ok(logs.some((m) => m.includes('env 键集缺失 2 项')), '缺失数量上屏');
    assert.ok(!calls.some((a) => a[0] === 'rm'), '旧容器未被移除');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('事故回归：file 新增键/改值（如试点 scope）→ 合法放行', async () => {
  const liveRaw = JSON.stringify({ Name: '/mp-console',
    Image: 'sha256:' + 'a'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:rc25', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser', 'RAGTRIAL_ALLOWED_SCOPES=old@b'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  const { dir, file } = writeInspect({ Name: '/mp-console',
    Image: 'sha256:' + 'a'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:rc25', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser', 'RAGTRIAL_ALLOWED_SCOPES=new@main'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  try {
    const logs = [];
    const calls = [];
    const code = await swapConsole([file, 'mp-console:rc26'], {
      exec: (args) => {
        calls.push(args);
        if (args[0] === 'inspect' && args[1] === 'mp-console') return { status: 0, stdout: liveRaw, stderr: '' };
        if (args[0] === 'image' && args[1] === 'inspect') return { status: 0, stdout: 'sha256:' + 'a'.repeat(64) + '\n', stderr: '' };
        if (args[0] === 'inspect' && args[1] === '--format') return { status: 0, stdout: 'sha256:' + 'a'.repeat(64) + '|true', stderr: '' };
        if (args[0] === 'exec') return { status: 0, stdout: 'HEALTH live v1 mu_ready=true', stderr: '' };
        return { status: 0, stdout: 'id\n', stderr: '' };
      },
      log: (m) => logs.push(m),
    });
    if (code !== 0) console.log('[dbg38]', JSON.stringify(logs));
    assert.equal(code, 0, '值差异与新增键放行：' + logs.join('|').slice(0, 200));
    assert.ok(logs.some((m) => m.includes('比对通过')));
    assert.ok(calls.some((a) => a[0] === 'rm'), '正常进入换版');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});

test('事故回归：Config.Image 兜底身份门——解析身份与原镜像不一致时拒绝兜底', async () => {
  // live Image=AAA（真实运行身份）；Config.Image 指向会解析为 BBB 的漂移 tag
  const liveRaw = JSON.stringify({ Name: '/mp-console',
    Image: 'sha256:' + 'a'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:drifted-tag', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  const { dir, file } = writeInspect({ Name: '/mp-console',
    Image: 'sha256:' + 'a'.repeat(64),
    HostConfig: { RestartPolicy: { Name: 'unless-stopped' } },
    Config: { Image: 'mp-console:drifted-tag', Env: ['CONSOLE_PG_DSN=postgres://pg/mu', 'MU_MODE=multiuser'] },
    Mounts: [], NetworkSettings: { Networks: {} } });
  try {
    const calls = [];
    const logs = [];
    const code = await swapConsole([file, 'mp-console:rc26'], {
      exec: (args) => {
        calls.push(args);
        if (args[0] === 'inspect' && args[1] === 'mp-console') return { status: 0, stdout: liveRaw, stderr: '' };
        if (args[0] === 'inspect' && args[1] === '--format') return { status: 0, stdout: 'sha256:' + 'a'.repeat(64) + '|false', stderr: '' };
        if (args[0] === 'image' && args[1] === 'inspect') {
          // 漂移 tag 解析为 BBB ≠ AAA
          const ref = args.slice(2).find((x) => !String(x).startsWith('--')) || '';
          return { status: 0, stdout: (ref.includes('sha256:' + 'a'.repeat(64)) ? 'sha256:' + 'a'.repeat(64) : 'sha256:' + 'b'.repeat(64)) + '\n', stderr: '' };
        }
        if (args[0] === 'run') return { status: 0, stdout: 'id\n', stderr: '' };
        if (args[0] === 'exec') return { status: 0, stdout: 'HEALTH live v1 mu_ready=false', stderr: '' };
        return { status: 0, stdout: '', stderr: '' };
      },
      log: (m) => logs.push(m),
    });
    // 场景：新容器 mu_ready=false 持续→主健康门 12 轮超时→回滚；回滚首选 ID=AAA
    // 但回滚探针同样 mu_ready=false→回滚失败；兜底 tag 解析身份=BBB≠AAA→身份门
    // 拒绝兜底（漂移 tag 不得恢复）→🛑 指引。断言：code 1 + 兜底拒绝上屏。
    assert.equal(code, 1);
    assert.ok(logs.some((m) => m.includes('Config.Image 兜底被拒绝')), '漂移 tag 兜底被身份门拒绝（上屏）');
    assert.ok(logs.some((m) => m.includes('mu_schema_ready 未就绪')), 'mu 门等待/失败可见');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
