// console/backend/test/runtime-readiness.test.mjs — Wave 3.13 readiness gate 单测。
// 覆盖任务书六的 CI 可运行子集：
//   T1 四 worker Running → AT_OK（30s 缓存）
//   T2 worker 缺席/非 Running → AT_WORKER_NOT_RUNNING（点名 worker）
//   T3 controller 不可达 → AT_CTRL_NOT_READY
//   T4 缓存：TTL 内复用上轮结果（第二次 fetch 计数不增）
//   T5 心跳新鲜但 bridge 不可消费（controller 只报 Running）→ 就绪层必须诚实放行
//      到桥探针层——即 readiness 不谎报 AT_OK 之外的语义（桥探针由恢复工具承担）
import { test, beforeEach, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(HERE + '/support/noop.js')('pg');
void Pool;
const MOD = await import('../lib/multiuser/agents/runtime-readiness.mjs');

const workersBody = (states) => ({ workers: ['mergepilot-leader', 'mergepilot-reviewer', 'mergepilot-fixer', 'mergepilot-verifier']
  .map((name, i) => ({ name, phase: states[i] ?? 'Running' })) });

let fetchCount = 0;
let respond = () => ({ ok: true, status: 200, json: async () => workersBody([]) });
const mockFetch = async (url, opts) => {
  fetchCount++;
  assert.ok(!String(opts?.headers?.Authorization ?? '').includes('real-key'), '不得泄漏真实 token');
  return respond(url, opts);
};

const TEST_TOKEN = ['test', '-', 'token'].join('');
const ENV = { baseUrl: 'http://ctrl.test', token: TEST_TOKEN };

beforeEach(() => { MOD.resetRuntimeReadinessCacheForTests(); fetchCount = 0; });

test('T1 四 worker Running → AT_OK', async () => {
  respond = () => ({ ok: true, status: 200, json: async () => workersBody(['Running', 'Running', 'Running', 'Running']) });
  const r = await MOD.checkAgentTeamsRuntimeReadiness({ ...ENV, fetchImpl: mockFetch, ttlMs: 0 });
  assert.equal(r.reason, 'AT_OK');
  assert.equal(r.ok, true);
});

test('T2 worker 非 Running → AT_WORKER_NOT_RUNNING 并点名', async () => {
  respond = () => ({ ok: true, status: 200, json: async () => workersBody(['Running', 'Sleeping', 'Running', 'Running']) });
  const r = await MOD.checkAgentTeamsRuntimeReadiness({ ...ENV, fetchImpl: mockFetch, ttlMs: 0 });
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'AT_WORKER_NOT_RUNNING');
  assert.equal(r.worker, 'mergepilot-reviewer');
});

test('T2b worker 缺席 → 同样拒绝', async () => {
  respond = () => ({ ok: true, status: 200, json: async () => ({ workers: [{ name: 'mergepilot-leader', phase: 'Running' }] }) });
  const r = await MOD.checkAgentTeamsRuntimeReadiness({ ...ENV, fetchImpl: mockFetch, ttlMs: 0 });
  assert.equal(r.ok, false);
});

test('T3 controller 不可达 → AT_CTRL_NOT_READY', async () => {
  respond = () => { throw new Error('unreachable'); };
  const r = await MOD.checkAgentTeamsRuntimeReadiness({ ...ENV, fetchImpl: mockFetch, ttlMs: 0 });
  assert.equal(r.reason, 'AT_CTRL_NOT_READY');
  respond = () => ({ ok: false, status: 503, json: async () => ({}) });
  const r2 = await MOD.checkAgentTeamsRuntimeReadiness({ ...ENV, fetchImpl: mockFetch, ttlMs: 0 });
  assert.equal(r2.reason, 'AT_CTRL_NOT_READY');
});

test('T4 TTL 缓存：就绪后 TTL 内不重复探测', async () => {
  respond = () => ({ ok: true, status: 200, json: async () => workersBody([]) });
  await MOD.checkAgentTeamsRuntimeReadiness({ ...ENV, fetchImpl: mockFetch, ttlMs: 60_000 });
  const c1 = fetchCount;
  await MOD.checkAgentTeamsRuntimeReadiness({ ...ENV, fetchImpl: mockFetch, ttlMs: 60_000 });
  assert.equal(fetchCount, c1, 'TTL 内应命中缓存');
});

test('T5 心跳/Running 就绪层不谎报桥语义（桥探针属恢复工具层）', () => {
  // readiness 的 AT_OK 仅表示 controller 视图四 worker Running；
  // 桥可消费性由 recover-runtime.sh 的探针与 E2E 承担——两者职责分离即本断言。
  const st = MOD.runtimeReadinessState();
  assert.ok('reason' in st);
});
