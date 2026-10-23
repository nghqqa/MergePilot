// test/start-mergepilot.test.mjs — 一键启动入口单元回归（#510 缺口修正波 + #512 行为闭环波）。
// 零 Docker：deps 全注入（exec/log/sleep/envGet/envSet/envDel）。
// 覆盖：参数组合校验（trial+iso 拒绝先于一切变更）/前置条件失败报告/
// 健康门（version+完整 commit+live+mu_ready）/执行器双探针/私钥注入与清理/
// iso 独立分派（首命令即 iso compose、零生产触达、显式镜像与环境、CWD 无关、
// 成功与失败双路径均恢复调用前进程 env）——断言真实生成的命令与环境，非仅参数合法性。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = path.join(HERE, '..', 'deploy', 'mergepilot-stack');
// 与 release-tools.test.mjs 同模式：临时副本注入 export（不改动源文件）
const src = fs.readFileSync(path.join(SRC, 'start-mergepilot.mjs'), 'utf8');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'start-mp-test-'));
const modFile = path.join(tmpDir, 'start-mergepilot.mjs');
fs.writeFileSync(modFile, src);

for (const f of ['docker-compose.trial.yml', 'docker-compose.yml', 'docker-compose.iso.yml', 'iso.env']) {
  fs.copyFileSync(path.join(SRC, f), path.join(tmpDir, f));
}
// iso 假秘密目录（与仓库内 iso-secrets 同构；基座 compose env_file 解析需文件存在）
fs.mkdirSync(path.join(tmpDir, 'iso-secrets'), { recursive: true });
fs.writeFileSync(path.join(tmpDir, 'iso-secrets', 'beta-mp-pg.env'), 'POSTGRES_PASSWORD=iso-dummy-not-used\n');
fs.writeFileSync(path.join(tmpDir, 'iso-secrets', 'beta-mp-console.env'), 'CONSOLE_SESSION_SECRET=iso-dummy-not-used\n');

const { startMergepilot, validateArgs, healthGateOk, executorGateOk, precheckFailures, RELEASE } =
  await import(pathToFileURL(modFile));

const GOOD_COMMIT = 'b7a0fcaf63ce3157a626c41918c8fdf60547ad74';

// ── 参数组合校验（先于一切变更）──
test('组合校验：trial+iso-preview 拒绝；未知环境拒绝；合法组合通过', () => {
  assert.equal(validateArgs({ environment: 'trial', isoPreview: true }).ok, false, 'trial+iso 拒绝');
  assert.equal(validateArgs({ environment: 'staging' }).ok, false, '未知环境拒绝');
  assert.equal(validateArgs({ environment: 'production' }).ok, true);
  assert.equal(validateArgs({ environment: 'trial' }).ok, true);
});

test('组合校验：拒绝发生在任何 docker 调用之前', async () => {
  let execCalls = 0;
  const code = await startMergepilot(['--environment', 'trial', '--iso-preview'], {
    exec: () => { execCalls++; return { status: 0, stdout: '', stderr: '' }; },
    log: () => {},
  });
  assert.equal(code, 2);
  assert.equal(execCalls, 0, '拒绝路径零 docker 调用');
});

// ── 健康门（纯函数）──
test('健康门：version/commit/live/mu_ready 全过才通过', () => {
  const good = { ok: true, version: 'v0.2.0-beta.6-rc.35.6-rc7', git_commit: GOOD_COMMIT,
    data_mode: 'live', mu_schema_ready: true };
  assert.equal(healthGateOk(good, { expectVersion: 'v0.2.0-beta.6-rc.35.6-rc7', expectCommit: GOOD_COMMIT }).ok, true);
  assert.equal(healthGateOk({ ...good, version: 'v0.2.0-beta.6-rc.35.6-rc6' },
    { expectVersion: 'v0.2.0-beta.6-rc.35.6-rc7' }).ok, false, '旧版本不通过');
  assert.equal(healthGateOk({ ...good, git_commit: 'c'.repeat(40) },
    { expectCommit: GOOD_COMMIT }).ok, false, '错误 commit 不通过');
  assert.equal(healthGateOk({ ...good, data_mode: 'snapshot' }, {}).ok, false, 'snapshot 不通过');
  assert.equal(healthGateOk({ ...good, mu_schema_ready: false }, {}).ok, false, 'mu_ready=false 不通过');
});

// ── 执行器双探针 ──
test('执行器门：workers+projects 双 200 才通过', () => {
  assert.equal(executorGateOk([{ probe: 'workers', status: 200 }, { probe: 'projects', status: 200 }]).ok, true);
  const bad = executorGateOk([{ probe: 'workers', status: 200 }, { probe: 'projects', status: 500 }]);
  assert.equal(bad.ok, false, '任一非 200 不通过');
});

// ── 前置条件：ctrl/manager start-only 前提缺失=明确报错 ──
test('precheck：ctrl/manager 容器缺失=明确报错（不宣称从零重建；exec 注入）', () => {
  const fakeExec = (args) => {
    if (args[0] === 'container' && args[1] === 'inspect') return { status: 1, stdout: '', stderr: 'no such container' };
    if (args[0] === 'volume' && args[1] === 'inspect') return { status: 0, stdout: 'vol', stderr: '' };
    return { status: 0, stdout: '', stderr: '' };
  };
  const { missing, stopped } = precheckFailures({
    secretFiles: [], modelManifest: null, volumes: [],
    containers: ['agentteams-beta-ctrl', 'agentteams-beta-manager'],
  }, fakeExec);
  assert.equal(missing.length, 2, '两个前置容器均报缺失');
  assert.equal(stopped.length, 0, '不存在的容器不进 stopped 列表');
  assert.ok(missing.every((m) => m.includes('start-only 前提')), '消息包含 start-only 前提声明');

  const stoppedCase = precheckFailures({ containers: ['agentteams-beta-ctrl'] }, (args) =>
    args[0] === 'container' ? { status: 0, stdout: 'exited|false', stderr: '' } : { status: 0, stdout: '', stderr: '' });
  assert.equal(stoppedCase.missing.length, 0, '存在但停止不报缺失');
  assert.deepEqual(stoppedCase.stopped, ['agentteams-beta-ctrl'], '停止容器进 stopped 由流程拉起');
});

// ── trial 一键：compose up 基础设施 + start 服务容器 + 健康门，零废弃服务复活 ──
test('trial 一键：基础设施 compose + start-only 容器 + 健康门（无 console-1/bge-m3 复活）', async () => {
  const calls = [];
  const logs = [];
  const isoTrialDir = fs.mkdtempSync(path.join(os.tmpdir(), 'trial-env-'));
  fs.writeFileSync(path.join(isoTrialDir, '.env'), 'POSTGRES_PASSWORD=iso-trial-pw');
  const code = await startMergepilot(['--environment', 'trial'], {
    exec: (args) => {
      calls.push(args);
      if (args[0] === 'volume' && args[1] === 'inspect') return { status: 0, stdout: 'vol', stderr: '' };
      if (args[0] === 'container' && args[2] === '--format') return { status: 0, stdout: 'false', stderr: '' };
      if (args[0] === 'exec') return { status: 0,
        stdout: JSON.stringify({ ok: true, version: 'v0.2.0-beta.6-rc.35.6-rc7',
          git_commit: GOOD_COMMIT, data_mode: 'live', mu_schema_ready: true }), stderr: '' };
      return { status: 0, stdout: '', stderr: '' };
    },
    log: (m) => logs.push(m),
    sleep: async () => {},
    trialDir: isoTrialDir,
  });
  assert.equal(code, 0);
  const up = calls.find((a) => a[0] === 'compose' && a[1] === '-p' && a[2] === 'mp-trial-rc355');
  assert.ok(up, '试用 compose up 已调用');
  assert.ok(up.join(' ').includes('docker-compose.trial.yml'), '使用仓库内 trial 真源 compose');
  const started = calls.filter((a) => a[0] === 'start').flatMap((a) => a.slice(1));
  assert.deepEqual(started.sort(), ['mp-trial-console', 'trial-bge'], '仅 start 两个服务容器');
  assert.ok(!calls.some((a) => a[0] === 'run'), '零 docker run（不从零重建）');
  assert.ok(!calls.some((a) => a[0] === 'create'), '零 create（不复活废弃服务）');
  assert.ok(logs.some((m) => m.includes('试用健康门通过')), '健康门通过上屏');
});

// ── #512 行为闭环：iso 独立分派（捕获真实命令与环境，非仅参数合法性）──

function isoDeps(calls, logs, procEnv) {
  return {
    exec: (args) => {
      calls.push(args);
      if (args[0] === 'exec') return { status: 0,
        stdout: JSON.stringify({ ok: true, version: 'v0.2.0-beta.6-rc.35.6-rc7',
          git_commit: GOOD_COMMIT, data_mode: 'live', mu_schema_ready: true }), stderr: '' };
      return { status: 0, stdout: '', stderr: '' };
    },
    log: (m) => logs.push(m),
    sleep: async () => {},
    envGet: (k) => (k in procEnv ? procEnv[k] : null),
    envSet: (k, v) => { procEnv[k] = v; },
    envDel: (k) => { delete procEnv[k]; },
  };
}

test('iso 一键：独立分派先于一切生产触达——首命令即 iso compose up，显式镜像与环境，CWD 无关', async () => {
  const calls = [], logs = [];
  const procEnv = { MU_GITHUB_APP_PRIVATE_KEY: 'caller-leftover' }; // 调用者残留私钥
  let envAtCompose = null; // compose 调用时刻的进程 env 快照
  const deps = isoDeps(calls, logs, procEnv);
  const baseExec = deps.exec;
  deps.exec = (args) => {
    if (args[0] === 'compose' && !envAtCompose) envAtCompose = { ...procEnv };
    return baseExec(args);
  };
  const prevCwd = process.cwd();
  process.chdir(os.tmpdir()); // 不同工作目录启动
  try {
    const code = await startMergepilot(['--environment', 'production', '--iso-preview'], deps);
    assert.equal(code, 0, 'iso 正式入口成功（当前版本无 iso 分支会走生产流而失败）');

    // 首条 docker 命令 = iso compose up（生产 precheck/agentteams 拉起不得先发生）
    assert.equal(calls[0][0], 'compose', '首命令即 compose（无任何前置 inspect/start）');
    assert.deepEqual(calls[0].slice(0, 2), ['compose', '-p']);
    assert.equal(calls[0][2], 'mergepilot-iso', '使用隔离项目名');
    const joined = calls[0].join(' ');
    assert.ok(joined.includes('docker-compose.yml') && joined.includes('docker-compose.iso.yml'),
      '基座+iso 覆盖双 -f');
    assert.ok(calls[0].includes(path.join(tmpDir, 'docker-compose.yml')),
      'compose 真源=脚本所在目录（与 CWD 无关）');

    // 全程零生产资源触达（容器名/卷/固定 IP 段）
    const all = calls.map((a) => a.join(' ')).join('\n');
    for (const forbidden of ['agentteams-beta-ctrl', 'agentteams-beta-manager', 'beta-mp-pg',
      'beta-mp-console', 'beta-bge-sidecar', 'at-auth-proxy', 'f2c57d69', 'prod-keystore-vol',
      '192.168.96.', 'beta-webhook-proxy']) {
      assert.ok(!all.includes(forbidden), '零生产触达: ' + forbidden);
    }

    // 环境（compose 调用时刻）：显式提供所需值，不依赖调用者残留
    assert.ok(envAtCompose, 'compose 已被调用');
    assert.equal(envAtCompose.CONSOLE_IMAGE, RELEASE.image, '未设 CONSOLE_IMAGE 时入口解析正确镜像');
    assert.equal(envAtCompose.SECRET_DIR, path.join(tmpDir, 'iso-secrets'), 'SECRET_DIR 指向 iso 假目录（非生产秘密）');
    assert.equal(envAtCompose.MU_GITHUB_APP_PRIVATE_KEY, '', '私钥显式空值——调用者残留不入隔离环境');
    // 运行结束后：调用者原值恢复、入口注入值清除
    assert.equal(procEnv.MU_GITHUB_APP_PRIVATE_KEY, 'caller-leftover', '调用者残留私钥恢复原值');
    assert.ok(!('CONSOLE_IMAGE' in procEnv), '入口注入且调用者未设的镜像变量已清除');
    assert.ok(!('SECRET_DIR' in procEnv), '入口注入且调用者未设的秘密目录已清除');

    // 健康门探针目标 = iso console
    assert.ok(calls.some((a) => a[0] === 'exec' && a[1] === 'mergepilot-iso-console'), '健康门探 iso console');
    assert.ok(logs.some((m) => m.includes('健康门通过')), 'iso 健康门通过上屏');
  } finally {
    process.chdir(prevCwd);
  }
});

test('环境恢复：成功与失败路径均还原调用前进程 env（残留值回填/未设值清除）', async () => {
  // 成功路径：caller 预置 CONSOLE_IMAGE 残留值
  const calls = [], logs = [];
  const procEnv = { CONSOLE_IMAGE: 'caller-junk' };
  const code = await startMergepilot(['--environment', 'production', '--iso-preview'],
    isoDeps(calls, logs, procEnv));
  assert.equal(code, 0);
  assert.equal(procEnv.CONSOLE_IMAGE, 'caller-junk', '成功路径恢复调用者原值');
  assert.ok(!('SECRET_DIR' in procEnv), '入口注入且调用者未设的变量已清除');
  assert.ok(!('MU_GITHUB_APP_PRIVATE_KEY' in procEnv), '入口注入且调用者未设的私钥已清除');

  // 失败路径：健康门必败（探针返回垃圾），48 轮注入 sleep=瞬时
  const calls2 = [], logs2 = [];
  const procEnv2 = { MU_GITHUB_APP_PRIVATE_KEY: 'caller-leftover' };
  const deps2 = isoDeps(calls2, logs2, procEnv2);
  deps2.exec = (args) => {
    calls2.push(args);
    if (args[0] === 'exec') return { status: 0, stdout: 'GARBAGE', stderr: '' };
    return { status: 0, stdout: '', stderr: '' };
  };
  const code2 = await startMergepilot(['--environment', 'production', '--iso-preview'], deps2);
  assert.equal(code2, 1, '健康门未过=失败退出');
  assert.equal(procEnv2.MU_GITHUB_APP_PRIVATE_KEY, 'caller-leftover', '失败路径恢复调用者原值');
  assert.ok(!('CONSOLE_IMAGE' in procEnv2), '失败路径清除入口注入值');
});

test('iso 配置预检：隔离文件缺失=exit 3 且零 docker 调用、零 env 注入', async () => {
  // 第二模块实例：目录内无 iso.env/iso-secrets（仅脚本与 trial compose）
  const bareDir = fs.mkdtempSync(path.join(os.tmpdir(), 'start-mp-bare-'));
  fs.writeFileSync(path.join(bareDir, 'start-mergepilot.mjs'), src);
  fs.copyFileSync(path.join(SRC, 'docker-compose.trial.yml'), path.join(bareDir, 'docker-compose.trial.yml'));
  const bare = await import(pathToFileURL(path.join(bareDir, 'start-mergepilot.mjs')));
  const calls = [], logs = [];
  const procEnv = {};
  const code = await bare.startMergepilot(['--environment', 'production', '--iso-preview'], {
    exec: (args) => { calls.push(args); return { status: 0, stdout: '', stderr: '' }; },
    log: (m) => logs.push(m),
    sleep: async () => {},
    envGet: (k) => (k in procEnv ? procEnv[k] : null),
    envSet: (k, v) => { procEnv[k] = v; },
    envDel: (k) => { delete procEnv[k]; },
  });
  assert.equal(code, 3, '隔离配置预检失败退出码 3');
  assert.equal(calls.length, 0, '零 docker 调用（预检先于一切变更）');
  assert.equal(Object.keys(procEnv).length, 0, '零 env 注入');
  assert.ok(logs.some((m) => m.includes('隔离配置预检失败')), '缺失清单上屏');
});
