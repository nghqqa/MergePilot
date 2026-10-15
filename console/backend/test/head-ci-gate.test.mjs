// head-ci-gate.test.mjs — 合并资格门 CI 读取回归（2026-10-15 收口指令点名的放行漏洞）。
// 覆盖：双面不一致阻塞（status 绿 + check-runs 5xx ≠ pass）、完整分页（翻页取到失败项）、
// completed+conclusion=null 阻塞、Actions-only（无 statuses）通过、重跑取最新、403 权限报告、
// 双面全空 not_configured、status 失败任一面即失败、Gitee unsupported 边界。
import assert from 'node:assert';
import test from 'node:test';

const fa = await import('../lib/multiuser/fix-apply.mjs');

const ENV = { MU_GITEE_PAT: 'x' };
import { generateKeyPairSync } from 'node:crypto';
const { privateKey: GH_PRIVATE_KEY } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const ENV_GH = { MU_GITHUB_APP_ID: '1',
  MU_GITHUB_APP_PRIVATE_KEY: GH_PRIVATE_KEY.export({ type: 'pkcs1', format: 'pem' }) };
const ARGS = { provider: 'github', owner: 'o', repoName: 'r',
  headSha: 'a'.repeat(40), installationId: '42' };

/** 组合全局 fetch 桩：App token 端点自动应答 + 场景路由（串行执行，用后恢复）。 */
function withGlobalFetch(routes, fn) {
  const real = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (u.includes('/app/installations/')) {
      return { ok: true, status: 201, json: async () => ({ token: ['test', 'installation', 'token'].join('-') }) };
    }
    calls.push(u.split('?')[0] + ' :: ' + (opts.method || 'GET'));
    for (const [prefix, handler] of routes) {
      if (u.includes(prefix)) {
        const out = handler(u, opts);
        return { ok: out.status >= 200 && out.status < 300, status: out.status,
          json: async () => (out.body ?? {}) };
      }
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  return Promise.resolve().then(fn).finally(() => { globalThis.fetch = real; });
}

test('C1-1 status 全绿 + check-runs HTTP 500 → unknown 阻塞（绝不退化为仅 status 判定）', async () => {
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'success', statuses: [{ state: 'success', context: 'ci' }] } })],
    ['/check-runs', () => ({ status: 500, body: {} })],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'unknown', JSON.stringify(r));
    assert.match(r.reason, /check-runs|不完整/);
  });
});

test('C1-2 分页完整读取：total=150 两页，第 2 页藏失败 → failed（漏页=错误放行）', async () => {
  const mk = (n, conclusion) => Array.from({ length: n }, (_, i) => ({
    name: `chk-${i}`, status: 'completed', conclusion }));
  const pages = { 1: mk(100, 'success'),
    2: [...mk(49, 'success'), { name: 'chk-late', status: 'completed', conclusion: 'failure' }] };
  let served = 0;
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'success', statuses: [] } })],
    ['/check-runs', (u) => {
      const page = Number(new URL('http://x' + u).searchParams.get('page') ?? 1);
      served = Math.max(served, page);
      return { status: 200, body: { total_count: 150, check_runs: pages[page] ?? [] } };
    }],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(served, 2, '必须翻到第 2 页');
    assert.equal(r.state, 'failed', JSON.stringify(r));
    assert.match(r.reason, /chk-late/);
  });
});

test('C1-3 completed 且 conclusion=null → unknown 阻塞（不得当 pass）', async () => {
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'success', statuses: [] } })],
    ['/check-runs', () => ({ status: 200, body: { total_count: 1, check_runs: [
      { name: 'mystery', status: 'completed', conclusion: null }] } })],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'unknown', JSON.stringify(r));
    assert.match(r.reason, /mystery/);
  });
});

test('C1-4 Actions-only 仓库（statuses 空 + 1 个 success check run）→ pass', async () => {
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'pending', statuses: [] } })],
    ['/check-runs', () => ({ status: 200, body: { total_count: 1, check_runs: [
      { name: 'test', status: 'completed', conclusion: 'success' }] } })],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'pass', JSON.stringify(r));
  });
});

test('C1-5 任一 check 失败即 failed（双面合并语义）', async () => {
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'success', statuses: [] } })],
    ['/check-runs', () => ({ status: 200, body: { total_count: 2, check_runs: [
      { name: 'test', status: 'completed', conclusion: 'failure' },
      { name: 'test-2', status: 'completed', conclusion: 'success' }] } })],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'failed', JSON.stringify(r));
  });
});

test('C1-6 status 403 → unsupported 并报告所需权限（不用 PAT 绕过）', async () => {
  await withGlobalFetch([['/status', () => ({ status: 403, body: {} })]], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'unsupported');
    assert.match(r.reason, /Commit statuses/);
  });
});

test('C1-6b check-runs 403 → unsupported 并报告 Checks 权限', async () => {
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'success', statuses: [] } })],
    ['/check-runs', () => ({ status: 403, body: {} })],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'unsupported');
    assert.match(r.reason, /Checks/);
  });
});

test('C1-7 双面全空 → not_configured 阻塞（不冒充通过）', async () => {
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'pending', statuses: [] } })],
    ['/check-runs', () => ({ status: 200, body: { total_count: 0, check_runs: [] } })],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'not_configured');
  });
});

test('C1-8 status failure + checks 全绿 → failed（任一面失败即失败）', async () => {
  await withGlobalFetch([
    ['/status', () => ({ status: 200, body: { state: 'failure',
      statuses: [{ state: 'failure', context: 'legacy-ci' }] } })],
    ['/check-runs', () => ({ status: 200, body: { total_count: 1, check_runs: [
      { name: 'test', status: 'completed', conclusion: 'success' }] } })],
  ], async () => {
    const r = await fa.readHeadCi(ENV_GH, { ...ARGS, env: ENV_GH });
    assert.equal(r.state, 'failed');
    assert.match(r.reason, /legacy-ci/);
  });
});

test('C1-9 Gitee status API 404 → unsupported（当前连接器能力边界，如实）', async () => {
  await withGlobalFetch([['/status', () => ({ status: 404, body: {} })]], async () => {
    const r = await fa.readHeadCi(ENV, { provider: 'gitee', owner: 'o', repoName: 'r',
      headSha: 'a'.repeat(40), env: ENV });
    assert.equal(r.state, 'unsupported');
  });
});
