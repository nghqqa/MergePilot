// console/backend/test/fxv-unit.test.mjs — FXV 编排状态机单元测试（内存 store 测试替身）。
// PG 语义（唯一约束/乐观并发/SKIP LOCKED）由 fxv-pg.integration.mjs 用真库覆盖。
// 运行：node --test console/backend/test/fxv-unit.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadFxvConfig, assertTargetAllowed } from '../lib/fxv/config.mjs';
import { createFxvStore } from '../lib/fxv/store.mjs';
import { transition, runPipeline, recover, STATES, TERMINAL, TransitionConflict, secretShapedIn } from '../lib/fxv/orchestrator.mjs';
import { makeWriteGate, GitHubWriteUnauthorized } from '../lib/fxv/github-gate.mjs';
import crypto from 'node:crypto';

function makeMemStore() {
  const attempts = new Map();
  const events = [];
  const grants = new Map();
  return {
    attempts, events, grants,
    async initSchema() {},
    async fileAttempt(a) {
      for (const [, row] of attempts) if (row.ticket_id === a.ticket_id) return { created: false, attempt: row };
      const row = { ...a, state: STATES.FILED, state_detail: {}, attempts_count: 0,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + (a.approval_ttl_ms ?? 86400_000)).toISOString() };
      attempts.set(a.attempt_id, row);
      events.push({ attempt_id: a.attempt_id, kind: 'FILED', to_state: 'FILED' });
      return { created: true, attempt: row };
    },
    async getAttempt(id) { return attempts.get(id) ?? null; },
    async getAttemptByTicket(t) { for (const [, r] of attempts) if (r.ticket_id === t) return r; return null; },
    async listNonTerminal(terminals) { return [...attempts.values()].filter((r) => !terminals.includes(r.state)); },
    async compareAndSetState(id, expected, next, detail) {
      const row = attempts.get(id);
      if (!row || row.state !== expected) return null;
      row.state = next; row.attempts_count += 1; row.updated_at = new Date().toISOString();
      row.state_detail = { ...(row.state_detail ?? {}), ...(detail ?? {}) };
      return { ...row };
    },
    async recordEvent(e) { events.push(e); },
    async listEvents(id) { return events.filter((e) => e.attempt_id === id); },
    async issueGrant(g) { if (!grants.has(g.grant_id)) grants.set(g.grant_id, { ...g, used_at: null, created_at: new Date().toISOString(), expires_at: new Date(Date.now() + g.ttl_ms).toISOString() }); return grants.get(g.grant_id); },
    async consumeGrant(repo) {
      for (const [k, g] of grants) {
        if (g.repo === repo && !g.used_at && new Date(g.expires_at) > new Date()) { g.used_at = new Date().toISOString(); return { ...g }; }
      }
      return null;
    },
    async activeGrantCount(repo) { let n = 0; for (const [, g] of grants) if (g.repo === repo && !g.used_at && new Date(g.expires_at) > new Date()) n++; return n; },
  };
}

const sha = () => crypto.randomBytes(20).toString('hex');
const digest = () => crypto.createHash('sha256').update(String(Math.random())).digest('hex');

function baseCfg(over = {}) {
  return { repoAllowlist: ['acme/app', 'acme/lib'], branchAllowlist: ['*'], dryRun: true,
    githubWrite: 'disabled', timeouts: { step_ms: 2000, approval_ttl_ms: 86400_000, grant_ttl_ms: 3600_000 }, maxRetries: 1, ...over };
}

async function filed(store, over = {}) {
  const a = { attempt_id: 'att-' + sha().slice(0, 8), ticket_id: 'tkt-' + sha().slice(0, 8),
    finding_id: 'fn-' + sha().slice(0, 8), repo: 'acme/app', branch: 'main',
    base_head_sha: sha(), patch_digest: digest(), actor: 'test', ...over };
  await store.fileAttempt(a);
  return store.getAttempt(a.attempt_id);
}

test('config: 空 allowlist fail-closed 拒绝运行', () => {
  const r = loadFxvConfig({ FXV_REPO_ALLOWLIST: '' });
  assert.equal(r.ok, false);
  assert.equal(r.reason, 'FXV_REPO_ALLOWLIST_REQUIRED');
});

test('config: dry-run 默认开启；GitHub 写默认 disabled', () => {
  const r = loadFxvConfig({ FXV_REPO_ALLOWLIST: 'a/b' });
  assert.equal(r.ok, true);
  assert.equal(r.config.dryRun, true);
  assert.equal(r.config.githubWrite, 'disabled');
  assert.equal(loadFxvConfig({ FXV_REPO_ALLOWLIST: 'a/b', FXV_DRY_RUN: '0' }).config.dryRun, false);
});

test('config: 非法 FXV_GITHUB_WRITE 值拒绝', () => {
  const r = loadFxvConfig({ FXV_REPO_ALLOWLIST: 'a/b', FXV_GITHUB_WRITE: 'yes' });
  assert.equal(r.ok, false);
});

test('allowlist: 越仓库/越分支拒绝', () => {
  const cfg = baseCfg({ branchAllowlist: ['main'] });
  assert.equal(assertTargetAllowed(cfg, 'acme/app', 'main').ok, true);
  assert.equal(assertTargetAllowed(cfg, 'other/repo', 'main').reason, 'REPO_NOT_IN_FXV_ALLOWLIST');
  assert.equal(assertTargetAllowed(cfg, 'acme/app', 'dev').reason, 'BRANCH_NOT_IN_FXV_ALLOWLIST');
});

test('transition: 非法转迁拒绝（FILED 不能直接 VERIFIED）', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await assert.rejects(
    transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.VERIFIED }),
    (e) => e instanceof TransitionConflict && e.info.reason_key === undefined && e.message === 'illegal_transition');
});

test('transition: 幂等重放同一转迁返回 idempotent', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  const r1 = await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  assert.equal(r1.idempotent, false);
  const r2 = await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  assert.equal(r2.idempotent, true);
});

test('transition: expected-state 不匹配拒绝（并发视角）', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await assert.rejects(
    transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.APPROVED }),
    (e) => e.message === 'expected_state_mismatch');
});

test('binding: head 漂移 → STALE_HEAD 终态 + 冲突异常', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  await assert.rejects(
    transition(store, { attemptId: a.attempt_id, from: STATES.APPROVED, to: STATES.PATCH_GENERATING, meta: { head_sha: sha() } }),
    (e) => e.message === 'stale_head');
  assert.equal((await store.getAttempt(a.attempt_id)).state, STATES.STALE_HEAD);
});

test('binding: patch digest 漂移 → DIGEST_DRIFT 终态', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  await assert.rejects(
    transition(store, { attemptId: a.attempt_id, from: STATES.APPROVED, to: STATES.PATCH_GENERATING, meta: { patch_digest: digest() } }),
    (e) => e.message === 'digest_drift');
  assert.equal((await store.getAttempt(a.attempt_id)).state, STATES.DIGEST_DRIFT);
});

test('pipeline: dry-run 默认全链到 DRY_RUN_COMPLETE（不发真实写）', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  const handlers = {
    generatePatch: async (cur) => ({ patch_text: '+ fix', patch_digest: cur.patch_digest }),
    dryRunApply: async () => ({ applied: true }),
    runTests: async () => ({ passed: true, output: 'ok' }),
  };
  const end = await runPipeline(store, baseCfg(), handlers, a.attempt_id);
  assert.equal(end, STATES.DRY_RUN_COMPLETE);
  const evs = await store.listEvents(a.attempt_id);
  assert.ok(evs.some((e) => e.to_state === STATES.DRY_RUN_VERIFIED));
  assert.ok(evs.some((e) => e.reason?.includes('dry_run=on')));
});

test('pipeline: handler 缺失 → MANUAL_WAIT（fail-closed，不伪成功）', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  const end = await runPipeline(store, baseCfg(), {}, a.attempt_id);
  assert.equal(end, STATES.MANUAL_WAIT);
});

test('pipeline: 隔离测试失败 → TEST_FAILED', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  const end = await runPipeline(store, baseCfg(), {
    generatePatch: async (cur) => ({ patch_text: '+ fix', patch_digest: cur.patch_digest }),
    dryRunApply: async () => ({ applied: true }),
    runTests: async () => ({ passed: false, output: 'assert failed' }),
  }, a.attempt_id);
  assert.equal(end, STATES.TEST_FAILED);
});

test('pipeline: 越权仓库 → ERROR_FATAL（allowlist 在入口强制）', async () => {
  const store = makeMemStore();
  const a = await filed(store, { repo: 'other/repo' });
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  const end = await runPipeline(store, baseCfg(), {}, a.attempt_id);
  assert.equal(end, STATES.ERROR_FATAL);
  const row = await store.getAttempt(a.attempt_id);
  assert.equal(row.state_detail.last_reason, 'REPO_NOT_IN_FXV_ALLOWLIST');
});

test('pipeline: 补丁含真实凭据形状 → ERROR_FATAL（PATCH_SECRET_SHAPED）', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  const badToken = 'ghp_' + 'x7Km2Lq'.repeat(5) + 'Aa9'; // 40 字符随机形
  const end = await runPipeline(store, baseCfg(), {
    generatePatch: async (cur) => ({ patch_text: `token = "${badToken}"`, patch_digest: cur.patch_digest }),
    dryRunApply: async () => ({ applied: true }),
    runTests: async () => ({ passed: true }),
  }, a.attempt_id);
  assert.equal(end, STATES.ERROR_FATAL);
  const row = await store.getAttempt(a.attempt_id);
  assert.match(row.state_detail.last_reason, /secret-shaped/);
});

test('pipeline: 步骤超时 → TIMEOUT 终态', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  const end = await runPipeline(store, baseCfg({ timeouts: { step_ms: 50, approval_ttl_ms: 86400_000, grant_ttl_ms: 3600_000 } }), {
    generatePatch: () => new Promise(() => {}), // 永不返回
    dryRunApply: async () => ({ applied: true }),
    runTests: async () => ({ passed: true }),
  }, a.attempt_id);
  assert.equal(end, STATES.TIMEOUT);
});

test('pipeline: dry_run=off 且无授权 → AWAITING_GITHUB_GRANT（人工等待）', async () => {
  const store = makeMemStore();
  const a = await filed(store);
  await transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: a.attempt_id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  const end = await runPipeline(store, baseCfg({ dryRun: false }), {
    generatePatch: async (cur) => ({ patch_text: '+ fix', patch_digest: cur.patch_digest }),
    dryRunApply: async () => ({ applied: true }),
    runTests: async () => ({ passed: true }),
    commitPush: async () => { throw new Error('must not reach'); },
  }, a.attempt_id);
  assert.equal(end, STATES.AWAITING_GITHUB_GRANT);
});

test('gate: githubWrite=disabled 时 consumePipelineGrant 返回 null；write gate assert 抛', async () => {
  const store = makeMemStore();
  const cfg = baseCfg();
  const { consumePipelineGrant } = await import('../lib/fxv/github-gate.mjs');
  await store.issueGrant({ grant_id: 'g1', operator: 'ngh', repo: 'acme/app', ttl_ms: 60000 });
  assert.equal((await consumePipelineGrant(cfg, store, 'acme/app')), null, 'disabled 模式不吃授权');
  const gate = makeWriteGate(cfg, store);
  await assert.rejects(gate.assert('acme/app'), GitHubWriteUnauthorized);
  const cfg2 = baseCfg({ githubWrite: 'authorized' });
  const g = await consumePipelineGrant(cfg2, store, 'acme/app');
  assert.equal(g.grant_id, 'g1');
  assert.equal((await consumePipelineGrant(cfg2, store, 'acme/app')), null, '授权一次性消费');
  const gate2 = makeWriteGate(cfg2, store);
  await gate2.assert('acme/app'); // authorized + allowlist → 通过
  await assert.rejects(gate2.assert('other/repo'), GitHubWriteUnauthorized);
});

test('recover: TTL 过期→EXPIRED；head 漂移→STALE_HEAD；其余 RESUMABLE', async () => {
  const store = makeMemStore();
  const a1 = await filed(store, { attempt_id: 'att-exp', ticket_id: 'tkt-exp' });
  const a2 = await filed(store, { attempt_id: 'att-stale', ticket_id: 'tkt-stale' });
  const a3 = await filed(store, { attempt_id: 'att-ok', ticket_id: 'tkt-ok' });
  // a1 过期
  const row1 = store.attempts.get('att-exp');
  row1.expires_at = new Date(Date.now() - 1000).toISOString();
  const report = await recover(store, baseCfg(), { currentHead: async (a) => a.attempt_id === 'att-stale' ? 'newhead' + sha().slice(0, 34) : a.base_head_sha });
  assert.equal(store.attempts.get('att-exp').state, STATES.EXPIRED);
  assert.equal(store.attempts.get('att-stale').state, STATES.STALE_HEAD);
  assert.equal(store.attempts.get('att-ok').state, STATES.FILED);
  const r3 = report.find((r) => r.attempt_id === 'att-ok');
  assert.equal(r3.action, 'RESUMABLE');
});

test('secretShapedIn: 合成 fixture 豁免、真实形状命中', () => {
  assert.equal(secretShapedIn('docs example AKIAIOSFODNN7EXAMPLE ok'), null);
  assert.equal(secretShapedIn('ghp_abcdefgh1234567890abcdefgh123456'), null);
  assert.match(secretShapedIn('const t = "ghp_' + 'Zz9Xx8Yy'.repeat(5) + '"'), /^ghp_/);
  assert.match(secretShapedIn('-----BEGIN ' + 'RSA PRIVATE KEY-----'), /PRIVATE KEY/);
});
