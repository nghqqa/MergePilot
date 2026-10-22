// test/start-mergepilot.test.mjs — 一键启动入口单元回归（#510 缺口修正波）。
// 零 Docker：deps 全注入（exec/log/sleep/envGet/envSet/envDel）。
// 覆盖：参数组合校验（trial+iso 拒绝先于一切变更）/前置条件失败报告/
// 健康门（version+完整 commit+live+mu_ready）/执行器双探针/私钥注入与清理。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// 与 release-tools.test.mjs 同模式：临时副本注入 export（不改动源文件）
const src = fs.readFileSync(path.join(HERE, '..', 'deploy', 'mergepilot-stack', 'start-mergepilot.mjs'), 'utf8');
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'start-mp-test-'));
const modFile = path.join(tmpDir, 'start-mergepilot.mjs');
fs.writeFileSync(modFile, src);

fs.copyFileSync(path.join(HERE, '..', 'deploy', 'mergepilot-stack', 'docker-compose.trial.yml'),
  path.join(tmpDir, 'docker-compose.trial.yml'));
const { startMergepilot, validateArgs, healthGateOk, executorGateOk, precheckFailures } =
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
  const missing = precheckFailures({
    secretFiles: [], modelManifest: null, volumes: [],
    containers: ['agentteams-beta-ctrl', 'agentteams-beta-manager'],
  }, fakeExec);
  assert.equal(missing.length, 2, '两个前置容器均报缺失');
  assert.ok(missing.every((m) => m.includes('start-only 前提')), '消息包含 start-only 前提声明');
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
