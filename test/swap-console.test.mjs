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

test('extraEnv 追加在位，且不覆盖继承键（继承值权威）', () => {
  const inspect = baseInspect({ Config: { Env: ['MERGEPILOT_VERSION=rc.11', 'EXISTING=keep'] } });
  const args = buildDockerRunArgs(inspect, 'img', { extraEnv: ['MERGEPILOT_VERSION=rc.12', 'NEWKEY=new'] });
  const es = valuesOf(args, '-e');
  assert.ok(es.includes('MERGEPILOT_VERSION=rc.11')); // 继承键不被覆盖
  assert.ok(!es.includes('MERGEPILOT_VERSION=rc.12'));
  assert.ok(es.includes('NEWKEY=new')); // 新键追加
  assert.ok(es.includes('EXISTING=keep'));
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

test('主入口：swap 序列 = rm -f 旧容器 → run -d + 继承参数（exec mock）', () => {
  const { dir, file } = writeInspect({
    Name: '/mp-console',
    HostConfig: { RestartPolicy: { Name: 'no' } },
    Config: { Env: ['A=1'] },
    Mounts: [{ Type: 'bind', Source: '/srv/mp/rag-models', Destination: '/models', Mode: 'ro', RW: false }],
  });
  try {
    const calls = [];
    const logs = [];
    const code = swapConsole([file, 'mp-console:rc12'], {
      exec: (args) => { calls.push(args); return { status: 0, stdout: 'deadbeef\n', stderr: '' }; },
      log: (m) => logs.push(m),
    });
    assert.equal(code, 0);
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[0], ['rm', '-f', 'mp-console']);
    assert.equal(calls[1][0], 'run');
    assert.equal(calls[1][1], '-d');
    assert.ok(calls[1].includes('mp-console:rc12'));
    assert.equal(flagValue(calls[1], '--restart'), 'unless-stopped');
    assert.ok(pairIn(calls[1], '-v', '/srv/mp/rag-models:/models:ro'));
    assert.ok(logs.some((m) => m.includes('docker run -d')));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('主入口：--dry-run 只打印命令，不调用 docker', () => {
  const { dir, file } = writeInspect(baseInspect());
  try {
    let execCalls = 0;
    const code = swapConsole([file, 'img', '--dry-run'], {
      exec: () => { execCalls++; return { status: 0, stdout: '', stderr: '' }; },
      log: () => {},
    });
    assert.equal(code, 0);
    assert.equal(execCalls, 0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('主入口：docker run 非零退出 → 透传退出码与 stderr', () => {
  const { dir, file } = writeInspect(baseInspect());
  try {
    const code = swapConsole([file, 'img'], {
      exec: (args) => (args[0] === 'run' ? { status: 125, stdout: '', stderr: 'docker: conflict\n' } : { status: 0, stdout: '', stderr: '' }),
      log: () => {},
    });
    assert.equal(code, 125);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('主入口：缺参 → 用法退出码 2；坏 JSON → 抛错不执行 docker', () => {
  let execCalls = 0;
  const deps = { exec: () => { execCalls++; return { status: 0, stdout: '', stderr: '' }; }, log: () => {} };
  assert.equal(swapConsole([], deps), 2);
  assert.equal(swapConsole(['only-inspect.json'], deps), 2);
  assert.equal(execCalls, 0);

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-console-test-'));
  const bad = path.join(dir, 'bad.json');
  fs.writeFileSync(bad, '{not json');
  try {
    assert.throws(() => swapConsole([bad, 'img'], deps), /inspect JSON 解析失败/);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  assert.equal(execCalls, 0);
});

test('主入口：inspect 数组形态（docker inspect 默认输出）→ 取首元素', () => {
  const { dir, file } = writeInspect([baseInspect({ HostConfig: { RestartPolicy: { Name: 'always' } } })]);
  try {
    let runArgs = null;
    const code = swapConsole([file, 'img'], {
      exec: (args) => { if (args[0] === 'run') runArgs = args; return { status: 0, stdout: 'id\n', stderr: '' }; },
      log: () => {},
    });
    assert.equal(code, 0);
    assert.equal(flagValue(runArgs, '--restart'), 'always');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
