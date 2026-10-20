// release-tools.test.mjs — 发布工具自动回归（rc.35.3 稳定性收口波）。
// 覆盖：swapConsole mock exec 隔离环境下的成功/失败/回滚语义、构建身份烙印
// 过滤、expect-commit 核对、探针参数传递与可区分诊断、schema init 有界重试。
// 隔离环境=注入 deps.exec（mock docker），不触生产、不跑真实容器。
import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..', '..');
const SWAP = path.join(ROOT, 'scripts', 'swap-console.cjs');
const PROBE = path.join(ROOT, 'scripts', 'swap-health-probe.js');

// swap-console.cjs 未导出内部函数——测试经临时副本注入 export（不改动源文件）
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'swap-test-'));
const swapMod = path.join(tmpDir, 'swap-console.cjs');
fs.writeFileSync(swapMod,
  fs.readFileSync(SWAP, 'utf8') + '\nmodule.exports = { swapConsole, buildDockerRunArgs, parseArgv };\n');
fs.copyFileSync(PROBE, path.join(tmpDir, 'swap-health-probe.js'));
const { swapConsole, buildDockerRunArgs } = await import(pathToFileURL(swapMod));

// ── 合成 inspect（生产形态最小集：Matrix env + DSN + 烙印 env + 挂载 + 双网络）──
const INSPECT = {
  Name: '/beta-mp-console',
  Config: {
    Image: 'ghcr.io/nghqqa/mergepilot-console@sha256:' + 'a'.repeat(64),
    Env: [
      'MERGEPILOT_VERSION=v0.2.0-beta.6-rc.34',
      'MERGEPILOT_GIT_COMMIT=' + 'b'.repeat(40),
      'CONSOLE_PG_DSN=postgres://x@pg/mu',
      'MU_MODE=multiuser',
      'MU_AGENTTEAMS_MATRIX_URL=http://ctrl:6167',
      'MU_AGENTTEAMS_MATRIX_USER=u1',
      'MU_AGENTTEAMS_MATRIX_PASSWORD=p1',
      'CONSOLE_PORT=4730',
    ],
  },
  HostConfig: { RestartPolicy: { Name: 'unless-stopped' }, PortBindings: {} },
  Mounts: [{ Source: 'D:/data/rag-models', Destination: '/app/rag-models', Mode: '', RW: true }],
  NetworkSettings: { Networks: { bridge: {}, agentteams_beta_atnet: {} } },
};
const NEW_IMAGE = 'ghcr.io/nghqqa/mergepilot-console@sha256:' + 'c'.repeat(64);
const GOOD_COMMIT = 'd'.repeat(40);

function writeInspect(obj) {
  const f = path.join(tmpDir, 'inspect-' + Math.random().toString(36).slice(2) + '.json');
  fs.writeFileSync(f, JSON.stringify(obj));
  return f;
}

/** mock docker exec/run——按子命令与脚本特征响应，记录全部调用 */
function makeMockDocker({ healthOut = 'HEALTH live v1 commit=' + GOOD_COMMIT.slice(0, 12) + ' bundle=index-x.js',
  idCommit = GOOD_COMMIT, matrixStatus = 200, precheckOut = null, liveInspect = null } = {}) {
  const calls = [];
  const exec = (args) => {
    calls.push(args);
    const out = (s) => ({ status: 0, stdout: s, stderr: '' });
    if (args[0] === 'exec') {
      const script = args.slice(4).join(' ');
      if (script.includes('PRECHECK')) return out(precheckOut ?? 'PRECHECK OK matrix-login 200');
      if (script.includes('m.login.password')) return out('STATUS ' + matrixStatus);
      if (script.includes('ID ')) return out('ID ' + idCommit + ' index-x.js');
      if (script.includes('HEALTH')) return out(healthOut);
      return out('');
    }
    if (args[0] === 'logs') return out('LOG-line-1\nLOG-listening');
    if (args[0] === 'inspect' && args[1] !== '--format') return out(JSON.stringify(liveInspect ?? INSPECT));
    if (args[0] === 'image' && args[1] === 'inspect') return out(INSPECT.Config.Image);
    return { status: 0, stdout: args[0] === 'run' ? 'container-id' : '', stderr: '' };
  };
  return { exec, calls };
}
const runCalls = (calls) => calls.filter((c) => c[0] === 'run');
const rmCalls = (calls) => calls.filter((c) => c[0] === 'rm');
const connectCalls = (calls) => calls.filter((c) => c[0] === 'network' && c[1] === 'connect');

// ── A. 正常切换：live+expect-commit 相符 → SWAP OK，烙印 env 被过滤，网络补挂 ──
test('A 正常切换：身份核对通过+烙印过滤+网络补挂+后置 Matrix', async () => {
  const { exec, calls } = makeMockDocker({ idCommit: GOOD_COMMIT });
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console',
    '--expect-commit=' + GOOD_COMMIT], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 0, 'exit 0, logs=' + logs.join('|').slice(0, 300));
  const run = runCalls(calls)[0];
  assert.ok(run, 'docker run called');
  // 烙印过滤：旧容器的 VERSION/GIT_COMMIT 不出现在新容器 env
  assert.equal(run.filter((a) => a === 'MERGEPILOT_VERSION=v0.2.0-beta.6-rc.34').length, 0, '旧 VERSION 被过滤');
  assert.equal(run.filter((a) => a.startsWith('MERGEPILOT_GIT_COMMIT=')).length, 0, '旧 GIT_COMMIT 被过滤');
  // 网络补挂（非默认网络）
  assert.ok(connectCalls(calls).some((c) => c[2] === 'agentteams_beta_atnet'), 'atnet connect');
  // 后置 Matrix 登录发生（exec 第二类脚本）
  assert.ok(calls.some((c) => c[0] === 'exec' && (c.slice(4).join(' ')).includes('m.login.password')), 'post matrix login');
  // expect-commit 核对发生
  assert.ok(calls.some((c) => c[0] === 'exec' && (c.slice(4).join(' ')).includes('ID ')), 'identity check');
});

// ── B. expect-commit 不符 → 拒绝交付+回滚（rm 新容器+run 旧镜像+connect 网络）──
// （2026-10-15 生产切换实证：health 报 8 位短哈希，原 40 位正则恒失败→自动回滚；
//   回滚流程本身按设计工作。前缀匹配修复见 B2/B3/B4。）
test('B expect-commit 不符：拒绝交付+回滚序列完整', async () => {
  const { exec, calls } = makeMockDocker({ idCommit: 'e'.repeat(40) }); // 与期望不符
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console',
    '--expect-commit=' + GOOD_COMMIT], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 1, 'exit 1（拒绝交付）');
  assert.equal(rmCalls(calls).filter((c) => c[2] === 'beta-mp-console').length >= 1, true, 'rm new');
  const rollbackRun = runCalls(calls).find((c) => c.includes(INSPECT.Config.Image));
  assert.ok(rollbackRun, 'rollback run uses old image');
  assert.ok(connectCalls(calls).some((c) => c[2] === 'agentteams_beta_atnet'), 'rollback network connect');
  assert.ok(logs.some((m) => m.includes('构建身份不符')), '拒绝原因可见');
});

// ── B2. health 报 8 位短哈希、为期望值前缀 → 通过（生产切换实证形态）──
test('B2 health 短哈希=期望前缀 → 构建身份核对通过', async () => {
  const { exec } = makeMockDocker({ idCommit: GOOD_COMMIT.slice(0, 8) });
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console',
    '--expect-commit=' + GOOD_COMMIT], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 0, '短哈希前缀匹配应通过');
  assert.ok(logs.some((m) => m.includes('构建身份核对通过')), '通过日志可见');
});

// ── B3. health 报全 40 位=期望值 → 仍通过（互为前缀）──
test('B3 health 全 40 位哈希 → 通过', async () => {
  const { exec } = makeMockDocker({ idCommit: 'f'.repeat(40) });
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console',
    '--expect-commit=' + 'f'.repeat(40)], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 0, '全哈希匹配应通过');
  assert.ok(logs.some((m) => m.includes('构建身份核对通过')), '通过日志可见');
});

// ── 执行器凭据预检查形状分派（2026-10-20 rc4 生产换版误判回归）──
const makeInspectEnv = (rows) => {
  const obj = JSON.parse(JSON.stringify(INSPECT));
  obj.Config.Env = rows;
  return writeInspect(obj);
};
const BASE_ENV = ['MERGEPILOT_VERSION=v0.2.0-beta.6-rc.34',
  'MERGEPILOT_GIT_COMMIT=' + 'b'.repeat(40), 'CONSOLE_PG_DSN=postgres://x@pg/mu', 'MU_MODE=multiuser'];
const MATRIX_PAIR_ENV = [...BASE_ENV, 'MU_AGENTTEAMS_MATRIX_URL=http://ctrl:6167',
  'MU_AGENTTEAMS_MATRIX_USER=u1', 'MU_AGENTTEAMS_MATRIX_PASSWORD=p1', 'CONSOLE_PORT=4730'];
const TOKEN_SHAPE_ENV = [...BASE_ENV, 'MU_AGENTTEAMS_MATRIX_URL=http://ctrl:6167',
  'MU_AGENTTEAMS_MATRIX_USER=u1', 'MU_AGENTTEAMS_MATRIX_PASSWORD=',
  'MU_AGENTTEAMS_BASE_URL=http://ctrl:8090', 'MU_AGENTTEAMS_TOKEN=tok-xyz', 'CONSOLE_PORT=4730'];
const UNCONFIG_ENV = [...BASE_ENV, 'MU_AGENTTEAMS_MATRIX_URL=http://ctrl:6167',
  'MU_AGENTTEAMS_MATRIX_USER=u1', 'MU_AGENTTEAMS_MATRIX_PASSWORD=', 'CONSOLE_PORT=4730'];

test('A2a TOKEN 形状（口令为空+TOKEN+BASE_URL）→ Bearer /api/v1/workers 通过并继续', async () => {
  const f = makeInspectEnv(TOKEN_SHAPE_ENV);
  const { exec, calls } = makeMockDocker({ precheckOut: 'PRECHECK OK token-workers 200' });
  const code = await swapConsole([f, NEW_IMAGE], { exec });
  assert.equal(code, 0);
  assert.ok(calls.some((c) => c[0] === 'exec' && c.slice(4).join(' ').includes('api/v1/workers')), '探针用 Bearer workers 端点');
  const precheckCalls = calls.filter((c) => c[0] === 'exec' && c.slice(4).join(' ').includes('PRECHECK'));
  assert.equal(precheckCalls.length, 2, '前置+后置各一次同款形状分派探针（不连发两次登录）');
});

test('A2b TOKEN 401 → AUTH 阻断 exit 3（与网络失败区分）', async () => {
  const f = makeInspectEnv(TOKEN_SHAPE_ENV);
  const { exec } = makeMockDocker({ precheckOut: 'PRECHECK AUTH token-workers 401' });
  assert.equal(await swapConsole([f, NEW_IMAGE], { exec }), 3);
});

test('A2c 网络/DNS 失败 → NETWORK 阻断（fail-closed）', async () => {
  const f = makeInspectEnv(TOKEN_SHAPE_ENV);
  const { exec } = makeMockDocker({ precheckOut: 'PRECHECK NETWORK token ENOTFOUND' });
  assert.equal(await swapConsole([f, NEW_IMAGE], { exec }), 3);
});

test('A2d 未配置且无 executor → UNCONFIGURED 如实放行（可选依赖）', async () => {
  const f = makeInspectEnv(UNCONFIG_ENV);
  const { exec } = makeMockDocker({ precheckOut: 'PRECHECK UNCONFIGURED optional' });
  assert.equal(await swapConsole([f, NEW_IMAGE], { exec }), 0);
});

test('A2e executor=agentteams 双凭证不可用 → EXECUTOR_MISMATCH 阻断', async () => {
  const f = makeInspectEnv([...UNCONFIG_ENV, 'MU_EXECUTOR=agentteams']);
  const { exec } = makeMockDocker({ precheckOut: 'PRECHECK EXECUTOR_MISMATCH executor' });
  assert.equal(await swapConsole([f, NEW_IMAGE], { exec }), 3);
});

test('A2g CLI 旗标单独生效（不经 deps）——解析值必须接线到门', async () => {
  const f = makeInspectEnv(TOKEN_SHAPE_ENV);
  const { exec, calls } = makeMockDocker({ precheckOut: 'PRECHECK AUTH token-workers 401' });
  const logs = [];
  // 只传 argv 旗标，不传 deps.skipMatrixCheck——回归锁：parseArgv 值必须真正生效
  const code = await swapConsole([f, NEW_IMAGE, '--skip-matrix-check'], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 0, '旗标未接线会被 AUTH 401 阻断（本断言防回归）');
  assert.ok(!calls.some((c) => c[0] === 'exec' && c.slice(4).join(' ').includes('PRECHECK')), '不发探针');
  assert.ok(logs.some((m) => m.includes('显式跳过')), '响亮记录');
});

test('A2h 纯 TOKEN 部署（无 MATRIX_* 键）→ 预检查照常运行并按 TOKEN 形状通过', async () => {
  const rows = ['MERGEPILOT_VERSION=v0.2.0-beta.6-rc.34',
    'MERGEPILOT_GIT_COMMIT=' + 'b'.repeat(40), 'CONSOLE_PG_DSN=postgres://x@pg/mu', 'MU_MODE=multiuser',
    'MU_AGENTTEAMS_BASE_URL=http://ctrl:8090', 'MU_AGENTTEAMS_TOKEN=tok-xyz', 'CONSOLE_PORT=4730'];
  const f = makeInspectEnv(rows);
  const live = JSON.parse(JSON.stringify(INSPECT));
  live.Config.Env = rows; // live 容器同为纯 TOKEN env（真实场景文件/live 一致）
  const { exec, calls } = makeMockDocker({ precheckOut: 'PRECHECK OK token-workers 200', liveInspect: live });
  const code = await swapConsole([f, NEW_IMAGE], { exec });
  assert.equal(code, 0, '任一认证 env 存在即触发预检查（不得因无 MATRIX 键而静默跳过）');
  assert.ok(calls.some((c) => c[0] === 'exec' && c.slice(4).join(' ').includes('api/v1/workers')), 'TOKEN 形状探针已发');
});

test('A2i 后置探针同口径：TOKEN 形状新容器校验失败 → 回滚（B4 同口径 exit 1）', async () => {
  const f = makeInspectEnv(TOKEN_SHAPE_ENV);
  const base = makeMockDocker({ precheckOut: 'PRECHECK OK token-workers 200' });
  let ran = false;
  const exec = (args) => {
    base.calls.push(args);
    if (args[0] === 'run') ran = true;
    if (ran && args[0] === 'exec' && args.slice(4).join(' ').includes('PRECHECK')) {
      return { status: 0, stdout: 'PRECHECK AUTH token-workers 401', stderr: '' };
    }
    if (ran && args[0] === 'inspect' && args[1] === '--format') {
      // 回滚后核验：旧镜像身份 + 运行中（makeMockDocker 未带该路由；fixture 顶层无
      // .Image——回滚身份取 Config.Image，与 base image-inspect 路由同源）
      return { status: 0, stdout: INSPECT.Config.Image + '|true', stderr: '' };
    }
    return base.exec(args);
  };
  const logs = [];
  const code = await swapConsole([f, NEW_IMAGE], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 1, '回滚成功=exit 1（切换未达成；B4 同口径）');
  assert.ok(logs.some((m) => m.includes('回滚完成')), '回滚健康核验通过');
  assert.ok(rmCalls(base.calls).length > 0, '旧容器已被 rm（回滚流程发生）');
  const probes = base.calls.filter((c) => c[0] === 'exec' && c.slice(4).join(' ').includes('PRECHECK'));
  assert.ok(probes.length >= 2, '前置+后置探针均发出（同口径）');
});

test('A2f --skip-matrix-check 显式跳过：不发起探针+响亮记录', async () => {
  const f = makeInspectEnv(TOKEN_SHAPE_ENV);
  const { exec, calls } = makeMockDocker({ precheckOut: 'PRECHECK AUTH token-workers 401' });
  const logs = [];
  const code = await swapConsole([f, NEW_IMAGE, '--skip-matrix-check'],
    { exec, log: (m) => logs.push(m), skipMatrixCheck: true });
  assert.equal(code, 0);
  assert.ok(!calls.some((c) => c[0] === 'exec' && c.slice(4).join(' ').includes('PRECHECK')), '不发探针');
  assert.ok(logs.some((m) => m.includes('显式跳过')), '响亮记录');
});

// ── B4. health 报 null → 回滚 ──
test('B4 health 报 null → 回滚', async () => {
  const { exec, calls } = makeMockDocker({ idCommit: null });
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console',
    '--expect-commit=' + GOOD_COMMIT], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 1);
  assert.ok(logs.some((m) => m.includes('构建身份不符')), '拒绝原因可见');
});

// ── C. live 门失败（探针返回 ERR）→ 失败诊断+回滚，不误判成功 ──
test('C live 门失败：失败容器日志诊断被调用+回滚', async () => {
  const { exec, calls } = makeMockDocker({ healthOut: 'HEALTH ERR fetch failed [exit=1 err=SyntaxError]' });
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console'], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 1, 'exit 1');
  assert.ok(calls.some((c) => c[0] === 'logs'), '失败日志诊断被调用');
  const rollbackRun = runCalls(calls).find((c) => c.includes(INSPECT.Config.Image));
  assert.ok(rollbackRun, 'rollback executed');
  // 挂载继承（回滚 run 含 inspect Mounts）
  assert.ok(rollbackRun.some((a) => String(a).includes('/app/rag-models')), 'mounts restored');
  assert.ok(connectCalls(calls).some((c) => c[2] === 'agentteams_beta_atnet'), 'rollback network restored');
  assert.ok(logs.some((m) => m.includes('data_mode 未达 live')), '失败原因可见');
});

// ── D. 探针自身损坏（stderr SyntaxError）→ 健康容器不被误判为 live ──
test('D 探针损坏：stderr 可见且不误判 live', async () => {
  const { exec, calls } = makeMockDocker({ healthOut: '' }); // exec 无 stdout
  const logs = [];
  // 直接操纵：把 mock 改为探针执行带 SyntaxError stderr
  const brokenExec = (args) => {
    calls.push(args);
    if (args[0] === 'exec' && (args.slice(4).join(' ')).includes('HEALTH')) {
      return { status: 1, stdout: '', stderr: '[eval]:1 SyntaxError: unexpected token' };
    }
    if (args[0] === 'exec' && (args.slice(4).join(' ')).includes('PRECHECK')) return { status: 1, stdout: '', stderr: '[eval]:1 SyntaxError: unexpected token' };
    if (args[0] === 'exec' && (args.slice(4).join(' ')).includes('ID ')) return { status: 0, stdout: 'ID ' + GOOD_COMMIT + ' x.js', stderr: '' };
    if (args[0] === 'logs') return { status: 0, stdout: 'LOG', stderr: '' };
    return { status: 0, stdout: 'cid', stderr: '' };
  };
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console'], { exec: brokenExec, log: (m) => logs.push(m) });
  assert.equal(code, 3, 'exit 3（预检查探针损坏=无 verdict，fail-closed 中止，不误判 live）');
  assert.equal(rmCalls(calls).length, 0, '旧容器未被 rm');
  assert.equal(runCalls(calls).length, 0, '无 docker run');
});

// ── E. 前置 Matrix 非 200 → 切换中止（exit 3），旧容器零触碰 ──
test('E 前置执行器凭据 AUTH（Matrix 403）：中止且旧容器零触碰', async () => {
  const { exec, calls } = makeMockDocker({ precheckOut: 'PRECHECK AUTH matrix-login 403' });
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console'], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 3, 'exit 3');
  assert.equal(rmCalls(calls).length, 0, '旧容器未被 rm');
  assert.equal(runCalls(calls).length, 0, '无 docker run');
});

// ── F. 探针文件：node 语法合法+Windows 数组传参下可执行到 fetch（参数不损坏）──
test('F 健康探针：语法合法+Windows 数组传参可区分诊断', () => {
  const chk = spawnSync(process.execPath, ['--check', PROBE], { encoding: 'utf8' });
  assert.equal(chk.status, 0, 'probe syntax: ' + chk.stderr);
  // 数组传参（无 shell）执行：探针会 fetch 失败——但失败形态必须是「运行时 HEALTH ERR」
  // 而非「SyntaxError」——即参数传递没有破坏探针结构。
  const r = spawnSync(process.execPath, ['-e', fs.readFileSync(PROBE, 'utf8')], { encoding: 'utf8', timeout: 8000 });
  const combined = (r.stdout || '') + (r.stderr || '');
  assert.ok(/HEALTH ERR/.test(combined), '执行到 catch（参数传递完好）: ' + combined.slice(0, 120));
  assert.ok(!/SyntaxError/.test(combined), '无语法破坏');
  // 语法损坏的探针 → 错误形态不同（可区分诊断）
  const broken = spawnSync(process.execPath, ['-e', 'fetch(((('], { encoding: 'utf8' });
  assert.ok(/SyntaxError/.test((broken.stderr || '')), '语法错误可区分');
});

// ── G. schema init 有界重试：失败后恢复/耗尽 fail-closed ──
test('G schema init 有界重试：恢复与耗尽', async () => {
  const api = await import(pathToFileURL(path.join(ROOT, "console", "backend", "lib", "multiuser", "api.mjs")));
  // 恢复：失败 2 次后成功
  let calls = 0;
  const attempts = [];
  const ok = await api.ensureMuReadyWithRetry({}, {
    max: 6, waitMs: 1, ensureFn: async () => {
      calls += 1;
      if (calls < 3) { const e = new Error('ENOTFOUND beta-mp-pg'); attempts.push(String(e.message)); throw e; }
      return {};
    },
    onAttempt: (i, max, err) => attempts.push(i + ':' + (err ? err.message.slice(0, 12) : 'ok')),
    sleep: async () => {},
  });
  assert.equal(ok, true, '恢复成功');
  assert.equal(calls, 3, '第三次成功');
  // 耗尽：全失败 → false 且恰好 max 次
  calls = 0;
  const exhausted = await api.ensureMuReadyWithRetry({}, {
    max: 4, waitMs: 1, ensureFn: async () => { calls += 1; throw new Error('down'); },
    sleep: async () => {},
  });
  assert.equal(exhausted, false, '耗尽返回 false（fail-closed）');
  assert.equal(calls, 4, '恰好 max 次（有界，不无限）');
});

// ── H. getMuStore rejected promise 不缓存（失败后可重试）——源码契约 ──
test('H getMuStore 失败清缓存（rejected promise 不永久）', () => {
  const src = fs.readFileSync(path.join(ROOT, 'console', 'backend', 'lib', 'multiuser', 'api.mjs'), 'utf8');
  const seg = src.slice(src.indexOf('muStorePromise = (async'), src.indexOf('return muStorePromise'));
  assert.ok(seg.includes('muStorePromise = null'), '失败路径清 muStorePromise（允许重试）');
});
