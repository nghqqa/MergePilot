// scripts/delivery/diagnose.test.mjs — 交付诊断工具单测（node:test，全 mock：无网络/无 docker/无真实 env）。
//
// 覆盖：env 解析、脱敏显示、callback 适用范围、检查项判定（fail/warn/pass/skip）、
// JSON 输出形状、凭据零输出红线（任何检查结果中不得出现 secret 原值）。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseEnvFile, displayValue, callbackScope, runChecks, DIAGNOSE_VERSION } from './diagnose.mjs';

const FULL_ENV = {
  POSTGRES_PASSWORD: 'pg-pass-123456',
  CONSOLE_SESSION_SECRET: 's'.repeat(44),
  MU_GITHUB_APP_ID: '123456',
  MU_GITHUB_APP_SLUG: 'my-mergepilot',
  MU_GITHUB_APP_PRIVATE_KEY: '-----BEGIN PRIVATE KEY---',
  MU_GITHUB_WEBHOOK_SECRET: 'w'.repeat(40),
  MU_GITHUB_APP_INSTALL_CALLBACK_URL: 'https://mp.example.com/multiuser',
  MU_GITHUB_OAUTH_CLIENT_ID: 'Iv1.clientid',
  MU_GITHUB_OAUTH_CLIENT_SECRET: 'oauth-secret-value',
  MU_GITHUB_OAUTH_CALLBACK_URL: 'https://mp.example.com/api/mu/auth/oauth/github/callback',
  MU_LLM_PROVIDER: 'openai_compatible',
  MU_LLM_BASE_URL: 'https://api.deepseek.example/v1',
  MU_LLM_MODEL: 'deepseek-chat',
  MU_LLM_API_KEY: 'sk-really-secret-key',
  MU_LLM_ALLOWED_MODELS: 'deepseek-flash,deepseek-chat',
  MU_FIX_TEST_CMDS: '{"gitee:demo/demo-repo":"npm test"}',
  MU_EXECUTOR: 'agentteams',
  MU_AGENTTEAMS_BASE_URL: 'http://controller:8090',
  MU_AGENTTEAMS_TOKEN: 'at-token-value',
  MU_AGENTTEAMS_RUNTIME: 'copaw',
  MU_AGENTTEAMS_MODEL: 'deepseek-chat',
  MU_AGENTTEAMS_MATRIX_URL: 'http://controller:6167',
  MU_AGENTTEAMS_MATRIX_USER: 'admin',
  MU_AGENTTEAMS_MATRIX_PASSWORD: 'matrix-pass',
  MU_GITEE_PAT: 'gitee-pat-value',
  MU_GITEE_OAUTH_CLIENT_ID: 'gid',
  MU_GITEE_OAUTH_CLIENT_SECRET: 'gsecret',
  MU_GITEE_OAUTH_CALLBACK_URL: 'https://mp.example.com/api/mu/auth/oauth/gitee/callback',
};

const SECRETS_IN_ENV = Object.values(FULL_ENV).filter((v) =>
  ['pg-pass-123456', 'oauth-secret-value', 'sk-really-secret-key', 'at-token-value', 'matrix-pass', 'gitee-pat-value'].includes(v));

/** 凭据红线断言：序列化后的检查结果不含任何 secret 原值。 */
function assertNoSecrets(checks) {
  const blob = JSON.stringify(checks);
  for (const s of SECRETS_IN_ENV) {
    assert.ok(!blob.includes(s), `检查结果泄露了 secret 值（${s.slice(0, 6)}…）`);
  }
}

test('parseEnvFile：解析 KEY=VALUE，忽略注释/空行，去引号', () => {
  const env = parseEnvFile('# 注释\nA=1\n\nB="two words"\nC=   spaced   \n');
  assert.equal(env.A, '1');
  assert.equal(env.B, 'two words');
  assert.equal(env.C, 'spaced');
});

test('displayValue：secret 只显示长度，非 secret 原样，空显示未设置', () => {
  assert.equal(displayValue('MU_LLM_API_KEY', 'sk-abcdef'), '已设置(9 字符)');
  assert.equal(displayValue('MU_LLM_BASE_URL', 'https://x.example/v1'), 'https://x.example/v1');
  assert.equal(displayValue('POSTGRES_PASSWORD', ''), '(未设置)');
  assert.equal(displayValue('MU_GITEE_PAT', undefined), '(未设置)');
});

test('callbackScope：https=pass / 回环=warn / http 非回环=warn / 缺失或坏值=fail', () => {
  assert.equal(callbackScope('https://mp.example.com/cb').level, 'pass');
  assert.equal(callbackScope('http://127.0.0.1:48500/cb').level, 'warn');
  assert.equal(callbackScope('http://192.168.1.5:48500/cb').level, 'warn');
  assert.equal(callbackScope('').level, 'fail');
  assert.equal(callbackScope('not-a-url').level, 'fail');
});

test('runChecks：配置齐全 + HTTP 全 200 + schema v28 → 无 fail、无 secret 泄露', async () => {
  const fakeFetch = async (url) => {
    if (String(url).endsWith('/api/health')) {
      return { status: 200, text: async () => JSON.stringify({ ok: true, version: '0.2.0-beta.6-rc.35.4', mu_schema_ready: true, git_commit: 'a'.repeat(40) }) };
    }
    if (String(url).endsWith('/api/mu/auth/providers')) {
      return { status: 200, text: async () => JSON.stringify({ github: { configured: true }, gitee: { configured: true }, fixture: { configured: false } }) };
    }
    return { status: 404, text: async () => '' };
  };
  const fakeDockerExec = () => ({ status: 0, stdout: '28\n', stderr: '' });
  const checks = await runChecks({ env: { ...FULL_ENV }, envFile: '/fake/.env', fetchImpl: fakeFetch, dockerExec: fakeDockerExec, pgContainer: 'pg-test', baseUrl: 'http://127.0.0.1:48500' });
  assertNoSecrets(checks);
  const fails = checks.filter((c) => c.status === 'fail');
  assert.deepEqual(fails, [], `不应有 fail 项：${JSON.stringify(fails)}`);
  const byId = Object.fromEntries(checks.map((c) => [c.id, c]));
  assert.equal(byId.db_schema.status, 'pass');
  assert.equal(byId.http_schema_ready.status, 'pass');
  assert.equal(byId.http_providers.status, 'pass');
  assert.equal(byId.fix_test_cmds.status, 'pass');
  assert.equal(byId.executor.status, 'pass');
  // 明细里 secret 键一律走脱敏显示
  assert.ok(!byId.env_core.detail.includes('pg-pass-123456'));
  assert.ok(byId.env_core.detail.includes('已设置'));
});

test('runChecks：全空 env → 核心项 fail 且 fix/ref 指向排障条目', async () => {
  const checks = await runChecks({ env: {}, envFile: null, fetchImpl: null, dockerExec: null, baseUrl: '' });
  assertNoSecrets(checks);
  const byId = Object.fromEntries(checks.map((c) => [c.id, c]));
  for (const id of ['env_core', 'env_github_app', 'env_oauth', 'env_llm', 'oauth_callback_scope']) {
    assert.equal(byId[id].status, 'fail', `${id} 应为 fail`);
    assert.ok(byId[id].fix, `${id} 应给处理步骤`);
    assert.ok(byId[id].ref, `${id} 应指向 TROUBLESHOOTING 条目`);
  }
  assert.equal(byId.node_version.status, 'pass');
  // 未提供 docker/base-url → 对应项 skip 而非 fail
  assert.equal(byId.db_schema.status, 'skip');
  assert.equal(byId.http_health.status, 'skip');
});

test('runChecks：schema 落后 → db_schema fail 并给出升级指引', async () => {
  const checks = await runChecks({ env: { ...FULL_ENV }, envFile: null, fetchImpl: null, dockerExec: () => ({ status: 0, stdout: '23\n', stderr: '' }), pgContainer: 'pg-x' });
  const c = checks.find((x) => x.id === 'db_schema');
  assert.equal(c.status, 'fail');
  assert.match(c.fix, /升级 console 镜像/);
});

test('runChecks：MU_FIX_TEST_CMDS 坏 JSON → fail；未设置 → warn（如实未执行）', async () => {
  const bad = await runChecks({ env: { ...FULL_ENV, MU_FIX_TEST_CMDS: '{not-json' }, envFile: null, fetchImpl: null });
  assert.equal(bad.find((c) => c.id === 'fix_test_cmds').status, 'fail');
  const none = await runChecks({ env: { ...FULL_ENV, MU_FIX_TEST_CMDS: '' }, envFile: null, fetchImpl: null });
  const c = none.find((x) => x.id === 'fix_test_cmds');
  assert.equal(c.status, 'warn');
  assert.match(c.detail, /未执行/);
});

test('runChecks：执行器三态——agentteams 缺项 fail / internal 未声明 fail / 非法值 fail', async () => {
  const at = await runChecks({ env: { ...FULL_ENV, MU_AGENTTEAMS_TOKEN: '' }, envFile: null, fetchImpl: null });
  const atC = at.find((c) => c.id === 'executor');
  assert.equal(atC.status, 'fail');
  assert.match(atC.detail, /MU_AGENTTEAMS_TOKEN/);
  const internal = await runChecks({ env: { ...FULL_ENV, MU_EXECUTOR: 'internal' }, envFile: null, fetchImpl: null });
  assert.equal(internal.find((c) => c.id === 'executor').status, 'fail');
  const invalid = await runChecks({ env: { ...FULL_ENV, MU_EXECUTOR: 'wat' }, envFile: null, fetchImpl: null });
  assert.equal(invalid.find((c) => c.id === 'executor').status, 'fail');
});

test('runChecks：health 不可达 → http_health fail；mu_schema_ready=false → http_schema_ready fail', async () => {
  const down = await runChecks({ env: { ...FULL_ENV }, envFile: null, fetchImpl: async () => { throw new Error('ECONNREFUSED'); }, baseUrl: 'http://127.0.0.1:1' });
  assert.equal(down.find((c) => c.id === 'http_health').status, 'fail');
  const notReady = await runChecks({ env: { ...FULL_ENV }, envFile: null,
    fetchImpl: async (url) => (String(url).endsWith('/api/health')
      ? { status: 200, text: async () => JSON.stringify({ ok: true, mu_schema_ready: false, mu_schema_note: 'schema init in progress' }) }
      : { status: 200, text: async () => JSON.stringify({ github: { configured: true }, gitee: { configured: false }, fixture: { configured: false } }) }),
    baseUrl: 'http://127.0.0.1:48500' });
  assert.equal(notReady.find((c) => c.id === 'http_schema_ready').status, 'fail');
});

test('runChecks：回环 callback → warn（仅本机可用），生产语义不误报 pass', async () => {
  const checks = await runChecks({ env: { ...FULL_ENV, MU_GITHUB_OAUTH_CALLBACK_URL: 'http://127.0.0.1:48500/api/mu/auth/oauth/github/callback' }, envFile: null, fetchImpl: null });
  const c = checks.find((x) => x.id === 'oauth_callback_scope');
  assert.equal(c.status, 'warn');
  assert.match(c.detail, /仅本机/);
});

test('runChecks：--no-net（fetchImpl=null）→ HTTP 项 skip；服务端视角未校验不冒充已验证', async () => {
  const checks = await runChecks({ env: { ...FULL_ENV }, envFile: null, fetchImpl: null, baseUrl: 'http://x' });
  assert.equal(checks.find((c) => c.id === 'http_health').status, 'skip');
  assert.equal(checks.find((c) => c.id === 'http_providers').status, 'skip');
});

test('版本常量与期望 schema 存在（升级时与 schema.mjs 同步）', () => {
  assert.ok(/^\d+\.\d+\.\d+$/.test(DIAGNOSE_VERSION));
});
