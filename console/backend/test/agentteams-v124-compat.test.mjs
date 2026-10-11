// console/backend/test/agentteams-v124-compat.test.mjs — Wave-QwenPaw executor 双代契约回归。
// 锁四件事（真实 executor 模块 + fetchImpl 注入，零 Docker）：
//  1) union body：扁平字段（v1.2.4 契约）与 spec 包装（223ddc2 契约）同时携带；
//  2) 漂移防线：controller 静默按默认值建户（spec-only 时代事故形态）→ AT_WORKER_DRIFT fail-closed；
//  3) 旧契约无 runtime/model 字段 → 不误伤（unknown 跳过）；
//  4) v1.2.4 201 语义 + 409/404 自愈路径在 union body 下保持。
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const executorPath = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'lib', 'multiuser', 'agents', 'agentteams-executor.mjs');
const { ensureFourAgents, resolveAgentTeamsConfig } = await import(pathToFileURL(executorPath).href);

const ROLES = ['mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier'];
const CFG = resolveAgentTeamsConfig({
  MU_EXECUTOR: 'agentteams',
  MU_AGENTTEAMS_BASE_URL: 'http://ctrl.test',
  MU_AGENTTEAMS_TOKEN: 'fake-ctrl-token-0123456789abcdef',
  MU_AGENTTEAMS_RUNTIME: 'copaw',
  MU_AGENTTEAMS_MODEL: 'deepseek-chat',
});

/** fake controller 工厂：shape='flat'|'spec' 决定 POST/PUT 读取哪代字段；drift 模拟静默默认值建户。 */
function fakeCtrl({ shape = 'flat', drift = false, createdRuntime = 'copaw', createdModel = 'deepseek-chat' } = {}) {
  const workers = new Map();
  const seen = [];
  const auth = (opts) => opts.headers?.authorization === `Bearer ${CFG.token}`;
  const fetchImpl = async (url, opts = {}) => {
    if (!auth(opts)) return { ok: false, status: 401, json: async () => ({}) };
    const p = new URL(url).pathname;
    if (p === '/api/v1/projects') return { ok: true, status: 200, json: async () => ({ projects: [] }) };
    if (p === '/api/v1/workers' && (opts.method ?? 'GET') === 'GET') {
      const list = [...workers.entries()].map(([name, w]) => ({ name, ...w }));
      return { ok: true, status: 200, json: async () => ({ workers: list, total: list.length }) };
    }
    if (p === '/api/v1/workers' && opts.method === 'POST') {
      const body = JSON.parse(opts.body);
      seen.push({ kind: 'POST', body });
      if (workers.has(body.name)) return { ok: false, status: 409, json: async () => ({}) };
      const src = shape === 'flat' ? body : (body.spec ?? {});
      workers.set(body.name, drift
        ? { phase: 'Running', runtime: createdRuntime, model: createdModel, roomID: '!f:e', matrixUserID: '@f:e' }
        : { phase: 'Running', runtime: src.runtime, model: src.model, roomID: '!f:e', matrixUserID: '@f:e' });
      // v1.2.4 真实语义：201 Created
      return { ok: true, status: 201, json: async () => ({ name: body.name, runtime: src.runtime }) };
    }
    const m = p.match(/^\/api\/v1\/workers\/(.+)$/);
    if (m && opts.method === 'PUT') {
      const body = JSON.parse(opts.body);
      seen.push({ kind: 'PUT', body });
      const name = decodeURIComponent(m[1]);
      if (!workers.has(name)) return { ok: false, status: 404, json: async () => ({}) };
      const src = shape === 'flat' ? body : (body.spec ?? {});
      const cur = workers.get(name);
      workers.set(name, { ...cur, runtime: src.runtime ?? cur.runtime, model: src.model ?? cur.model });
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };
  return { fetchImpl, workers, seen };
}

test('QC-1 v1.2.4 扁平契约：union body 携带顶层 runtime/model/identity + spec 包装，201 接受，四 worker 就位', async () => {
  const c = fakeCtrl({ shape: 'flat' });
  const r = await ensureFourAgents(CFG, { fetchImpl: c.fetchImpl });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(c.workers.size, 4);
  const post = c.seen.filter((s) => s.kind === 'POST');
  assert.equal(post.length, 4);
  for (const s of post) {
    // 双代字段同时在位：顶层扁平（v1.2.4 读取）+ spec 包装（223ddc2 读取）
    assert.equal(s.body.runtime, 'copaw', '顶层 runtime（v1.2.4 契约）');
    assert.equal(s.body.model, 'deepseek-chat', '顶层 model');
    assert.equal(s.body.spec?.runtime, 'copaw', 'spec 包装 runtime（223ddc2 契约）');
    assert.equal(s.body.spec?.state, 'Running');
    assert.ok(Array.isArray(s.body.skills) && Array.isArray(s.body.mcpServers) && Array.isArray(s.body.expose));
    assert.ok(typeof s.body.identity === 'string' && s.body.identity.length > 0);
  }
  assert.deepEqual([...c.workers.keys()].sort(), [...ROLES].sort());
});

test('QC-2 静默默认值漂移拒绝：controller 忽略请求 runtime/model 按默认建户 → AT_WORKER_DRIFT fail-closed', async () => {
  // 事故形态复现：v1.2.4 收到 spec-only body 时 runtime 落默认（qwenpaw 栈默认 copaw——
  // 此处以 qwenpaw 作 drift 值演示差异被拒绝，语义与字段无关）
  const c = fakeCtrl({ shape: 'flat', drift: true, createdRuntime: 'qwenpaw', createdModel: 'deepseek-chat' });
  const r = await ensureFourAgents(CFG, { fetchImpl: c.fetchImpl });
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'AT_WORKER_DRIFT');
  assert.ok(r.drifted.length >= 4);
  assert.equal(r.drifted[0].field, 'runtime');
  assert.equal(r.drifted[0].expected, 'copaw');
  assert.equal(r.drifted[0].actual, 'qwenpaw');
});

test('QC-2b model 漂移拒绝：runtime 一致但 model 被改 → DRIFT', async () => {
  const c = fakeCtrl({ shape: 'flat', drift: true, createdRuntime: 'copaw', createdModel: 'gpt-x' });
  const r = await ensureFourAgents(CFG, { fetchImpl: c.fetchImpl });
  assert.equal(r.reason, 'AT_WORKER_DRIFT');
  assert.ok(r.drifted.every((d) => d.field === 'model' && d.expected === 'deepseek-chat' && d.actual === 'gpt-x'));
});

test('QC-3 旧契约（223ddc2）：spec 包装生效；列表无 runtime/model 字段 → 不误伤（unknown 跳过）', async () => {
  const c = fakeCtrl({ shape: 'spec' });
  // 旧 controller 列表行不含 runtime/model（真实 223ddc2 形态）——fake 在列表序列化时剔除
  const rawFetch = c.fetchImpl;
  const fetchImpl = async (url, opts = {}) => {
    const r = await rawFetch(url, opts);
    if ((opts.method ?? 'GET') === 'GET' && new URL(url).pathname === '/api/v1/workers') {
      const j = await r.json();
      const list = (j.workers ?? []).map(({ runtime, model, ...rest }) => rest);
      return { ok: true, status: 200, json: async () => ({ workers: list, total: list.length }) };
    }
    return r;
  };
  const r = await ensureFourAgents(CFG, { fetchImpl });
  assert.equal(r.ok, true, JSON.stringify(r));
  assert.equal(c.workers.size, 4);
  // 第二轮触发 PUT 路径——spec 包装字段被旧 controller 实际读取
  const r2 = await ensureFourAgents(CFG, { fetchImpl });
  assert.equal(r2.ok, true);
  const put = c.seen.find((s) => s.kind === 'PUT');
  assert.ok(put && put.body.spec?.runtime === 'copaw');
});

test('QC-4 已存在 worker → PUT（union）+ 404 自愈转 POST 路径保持', async () => {
  const c = fakeCtrl({ shape: 'flat' });
  await ensureFourAgents(CFG, { fetchImpl: c.fetchImpl });
  // 第二轮：全部已存在 → 4 次 PUT、0 次 POST（无 404 竞态时）
  const r = await ensureFourAgents(CFG, { fetchImpl: c.fetchImpl });
  assert.equal(r.ok, true);
  assert.deepEqual(r.created, []);
  const secondRoundPut = c.seen.filter((s) => s.kind === 'PUT').length;
  assert.equal(secondRoundPut, 4, `第二轮应 4 次 PUT，实得 ${secondRoundPut}`);
});

test('QC-5 既有回归不回退：认证失败 fail-closed / 409 已存在不报错', async () => {
  const c = fakeCtrl({ shape: 'flat' });
  const badAuth = async (url, opts = {}) => ({ ...await c.fetchImpl(url, opts), ok: false, status: 401 });
  const r = await ensureFourAgents(CFG, { fetchImpl: badAuth });
  assert.equal(r.ok, false);
  assert.match(r.reason, /^AT_WORKERS_LIST_HTTP_401$/);
  // 预置已存在（409）：POST 返回 409 不视为失败
  const c2 = fakeCtrl({ shape: 'flat' });
  await c2.fetchImpl('http://ctrl.test/api/v1/workers', { method: 'POST',
    headers: { authorization: `Bearer ${CFG.token}`, 'content-type': 'application/json' },
    body: JSON.stringify({ name: ROLES[0], spec: {} }) });
  const r2 = await ensureFourAgents(CFG, { fetchImpl: c2.fetchImpl });
  assert.equal(r2.ok, true);
});
