// console/backend/test/agentteams-recovery.test.mjs —
// deploy/agentteams-beta/auth-proxy/recover-agentteams.mjs 恢复核心回归（零 Docker）。
//
// 背景（2026-10-08 实证）：ctrl 容器【重建】清空容器 FS 内的 worker 注册表；恢复工具
// 复用生产契约 ensureFourAgents（GET 判存在→缺者 POST/在者 PUT→完整性复查）。
// 本测试以内存态 fake controller 锁定：
//  * 幂等——重复运行不产生重复注册（第二次 created=[]，注册数恒 4）；
//  * fail-closed——认证失败（401）不发生任何写操作；注册不完整即失败并报缺失；
//  * 摘要卫生——返回结构绝不含凭据形态字段。
// 真实 executor 模块（非 mock 副本）经 AT_EXECUTOR_MODULE 默认仓库相对路径加载，
// fetch 以 fetchImpl 注入——与生产同一代码路径。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { recoverWorkerRegistry } from '../../../deploy/agentteams-beta/auth-proxy/recover-agentteams.mjs';

const ROLES = ['mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'];
const ENV = {
  MU_EXECUTOR: 'agentteams',
  MU_AGENTTEAMS_BASE_URL: 'http://at-auth-proxy:8091',
  MU_AGENTTEAMS_TOKEN: 'fake-ctrl-token-0123456789abcdef', // 合成 fixture（secret-scan 豁免词 fake）
  MU_AGENTTEAMS_RUNTIME: 'copaw',
  MU_AGENTTEAMS_MODEL: 'deepseek-chat',
};

/** 内存态 fake controller：复刻 223ddc2 真实契约（POST 创建/409 已存在、PUT update-only/404 不存在）。 */
function fakeCtrl({ hideFromList = () => false } = {}) {
  const workers = new Map();
  const calls = { post: 0, put: 0, healthy: 0 };
  const fetchImpl = async (url, opts = {}) => {
    const u = new URL(url);
    const p = u.pathname;
    const auth = opts.headers?.authorization ?? '';
    if (auth !== `Bearer ${ENV.MU_AGENTTEAMS_TOKEN}`) {
      return { ok: false, status: 401, json: async () => ({ message: 'unauthorized' }) };
    }
    if (p === '/api/v1/projects') { calls.healthy++; return { ok: true, status: 200, json: async () => ({ projects: [], total: 0 }) }; }
    if (p === '/api/v1/workers' && (opts.method ?? 'GET') === 'GET') {
      const list = [...workers.entries()].filter(([name]) => !hideFromList(name))
        .map(([name, w]) => ({ name, ...w }));
      return { ok: true, status: 200, json: async () => ({ workers: list, total: list.length }) };
    }
    if (p === '/api/v1/workers' && opts.method === 'POST') {
      calls.post++;
      const body = JSON.parse(opts.body);
      if (workers.has(body.name)) return { ok: false, status: 409, json: async () => ({}) };
      workers.set(body.name, { phase: 'Running', roomID: '!fake:example', matrixUserID: '@fake:example' });
      return { ok: true, status: 200, json: async () => ({}) };
    }
    const m = p.match(/^\/api\/v1\/workers\/(.+)$/);
    if (m && opts.method === 'PUT') {
      calls.put++;
      if (!workers.has(decodeURIComponent(m[1]))) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  return { fetchImpl, workers, calls };
}

test('恢复：空注册表首跑建 4，重复运行幂等（不产生重复注册）', async () => {
  const fake = fakeCtrl();
  const r1 = await recoverWorkerRegistry({ env: ENV, fetchImpl: fake.fetchImpl });
  assert.equal(r1.ok, true);
  assert.deepEqual([...r1.created].sort(), [...ROLES].sort());
  assert.equal(r1.workerCount, 4);
  assert.equal(fake.calls.post, 4);

  const r2 = await recoverWorkerRegistry({ env: ENV, fetchImpl: fake.fetchImpl });
  assert.equal(r2.ok, true);
  assert.deepEqual(r2.created, [], 'second run must not re-create (idempotent)');
  assert.equal(r2.workerCount, 4);
  assert.equal(fake.workers.size, 4, 'registry size stays 4 — no duplicate registration');
  assert.equal(fake.calls.post, 4, 'POST count unchanged on second run');
  assert.ok(fake.calls.put >= 4, 'existing workers go through update-only PUT path');
  assert.equal(r2.note, 'registry-restored-4/4');
});

test('恢复：认证失败 fail-closed——健康检查即止，零写操作', async () => {
  const calls = { total: 0, post: 0 };
  const deny = async (url, opts = {}) => {
    calls.total++;
    if ((opts.method ?? 'GET') === 'POST') calls.post++;
    return { ok: false, status: 401, json: async () => ({}) };
  };
  const r = await recoverWorkerRegistry({ env: ENV, fetchImpl: deny });
  assert.equal(r.ok, false);
  assert.equal(r.stage, 'healthy');
  assert.equal(r.reason, 'AT_AUTH_FAILED');
  assert.equal(calls.post, 0, 'no write may happen when unhealthy');
  assert.ok(calls.total <= 2, 'recovery must stop at the health check');
});

test('恢复：注册不完整 fail-closed——报缺失清单，不假宣称成功', async () => {
  // controller 接受创建但从列表隐藏 verifier（模拟半建/竞态）
  const fake = fakeCtrl({ hideFromList: (n) => n === 'mergepilot-verifier' });
  const r = await recoverWorkerRegistry({ env: ENV, fetchImpl: fake.fetchImpl });
  assert.equal(r.ok, false);
  assert.equal(r.stage, 'ensure');
  assert.equal(r.reason, 'AT_WORKERS_INCOMPLETE');
  assert.deepEqual(r.missing, ['mergepilot-verifier']);
});

test('恢复：执行器模式未配置 fail-closed（EXECUTOR_MODE_UNSET，零请求）', async () => {
  let fetches = 0;
  const counting = async () => { fetches++; return { ok: false, status: 599, json: async () => ({}) }; };
  const r = await recoverWorkerRegistry({ env: { MU_EXECUTOR: '' }, fetchImpl: counting });
  assert.equal(r.ok, false);
  assert.equal(r.stage, 'healthy');
  assert.equal(r.reason, 'EXECUTOR_MODE_UNSET');
  assert.equal(fetches, 0);
});

test('恢复：摘要卫生——结构化输出不含凭据形态内容', async () => {
  const fake = fakeCtrl();
  const r = await recoverWorkerRegistry({ env: ENV, fetchImpl: fake.fetchImpl });
  const s = JSON.stringify(r);
  assert.ok(!s.includes('Bearer'), 'summary must not contain Bearer scheme');
  assert.ok(!s.includes(ENV.MU_AGENTTEAMS_TOKEN), 'summary must not contain credential value');
  assert.ok(!/eyJ[A-Za-z0-9_-]{10,}/.test(s), 'summary must not contain JWT-like literals');
});
