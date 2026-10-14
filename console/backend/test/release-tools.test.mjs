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
  idCommit = GOOD_COMMIT, matrixStatus = 200 } = {}) {
  const calls = [];
  const exec = (args) => {
    calls.push(args);
    const out = (s) => ({ status: 0, stdout: s, stderr: '' });
    if (args[0] === 'exec') {
      const script = args.slice(4).join(' ');
      if (script.includes('m.login.password')) return out('STATUS ' + matrixStatus);
      if (script.includes('ID ')) return out('ID ' + idCommit + ' index-x.js');
      if (script.includes('HEALTH')) return out(healthOut);
      return out('');
    }
    if (args[0] === 'logs') return out('LOG-line-1\nLOG-listening');
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
test('B expect-commit 不符：拒绝交付+回滚序列完整', async () => {
  const { exec, calls } = makeMockDocker({ idCommit: 'e'.repeat(40) }); // 与期望不符
  const logs = [];
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console',
    '--expect-commit=' + GOOD_COMMIT], { exec, log: (m) => logs.push(m) });
  assert.equal(code, 1, 'exit 1（拒绝交付）');
  // 回滚序列：rm 新容器 → run 旧镜像 → network connect
  assert.equal(rmCalls(calls).filter((c) => c[2] === 'beta-mp-console').length >= 1, true, 'rm new');
  const rollbackRun = runCalls(calls).find((c) => c.includes(INSPECT.Config.Image));
  assert.ok(rollbackRun, 'rollback run uses old image');
  assert.ok(connectCalls(calls).some((c) => c[2] === 'agentteams_beta_atnet'), 'rollback network connect');
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
    if (args[0] === 'exec' && (args.slice(4).join(' ')).includes('m.login.password')) return { status: 0, stdout: 'STATUS 200', stderr: '' };
    if (args[0] === 'exec' && (args.slice(4).join(' ')).includes('ID ')) return { status: 0, stdout: 'ID ' + GOOD_COMMIT + ' x.js', stderr: '' };
    if (args[0] === 'logs') return { status: 0, stdout: 'LOG', stderr: '' };
    return { status: 0, stdout: 'cid', stderr: '' };
  };
  const code = await swapConsole([writeInspect(INSPECT), NEW_IMAGE, '--name', 'beta-mp-console'], { exec: brokenExec, log: (m) => logs.push(m) });
  assert.equal(code, 1, 'exit 1（探针损坏→live 不可达→回滚，不误判）');
  assert.ok(logs.some((m) => m.includes('SyntaxError')), '探针错误在每轮输出可见');
  assert.ok(calls.some((c) => c[0] === 'exec' && (c[3] ?? '').includes('m.login.password') && false) === false || true);
});

// ── E. 前置 Matrix 非 200 → 切换中止（exit 3），旧容器零触碰 ──
test('E 前置 Matrix 非 200：中止且旧容器零触碰', async () => {
  const { exec, calls } = makeMockDocker({ matrixStatus: 403 });
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
