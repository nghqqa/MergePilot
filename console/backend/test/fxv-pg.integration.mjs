#!/usr/bin/env node
// console/backend/test/fxv-pg.integration.mjs — FXV 真 PG + 真 git 集成回归（P7 十一场景载体）。
// 运行：node console/backend/test/fxv-pg.integration.mjs
//   环境依赖：docker（postgres:16-alpine 本地镜像）、git；若 FXV_PG_TEST_DSN 已设则直连该库。
// 真实性边界：PG 为真库（临时容器）；git remote 为本地 bare 仓库路径——绝不触达 GitHub。
import { execFileSync, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createFxvStore } = await import('../lib/fxv/store.mjs');
const { transition, runPipeline, recover, STATES, TransitionConflict } = await import('../lib/fxv/orchestrator.mjs');
const { consumePipelineGrant } = await import('../lib/fxv/github-gate.mjs');
const { loadFxvConfig } = await import('../lib/fxv/config.mjs');

const sha = () => crypto.randomBytes(20).toString('hex');
const digestOf = (t) => crypto.createHash('sha256').update(t).digest('hex');
let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + detail : ''}`); }
}
const git = (cwd, ...args) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', ...args], { cwd, encoding: 'utf8' });

// ── PG 生命周期 ──
let dsn = process.env.FXV_PG_TEST_DSN, container = null;
async function startPg() {
  if (dsn) return;
  container = 'fxv-pg-test-' + sha().slice(0, 6);
  execFileSync('docker', ['run', '-d', '--rm', '--name', container,
    '-e', 'POSTGRES_PASSWORD=fxv', '-e', 'POSTGRES_DB=fxv', '-p', '127.0.0.1::5432', 'postgres:16-alpine'], { encoding: 'utf8' });
  const portOut = execFileSync('docker', ['port', container, '5432/tcp'], { encoding: 'utf8' }).trim();
  const port = portOut.split(':').pop();
  dsn = `postgres://postgres:fxv@127.0.0.1:${port}/fxv`;
  for (let i = 0; i < 60; i++) {
    const r = spawnSync('docker', ['exec', container, 'pg_isready', '-U', 'postgres'], { encoding: 'utf8' });
    if (r.status === 0) return;
    await new Promise((r2) => setTimeout(r2, 500));
  }
  throw new Error('postgres not ready');
}
async function stopPg() {
  if (container) { try { execFileSync('docker', ['rm', '-f', container]); } catch { /* best effort */ } }
}

// ── 真 git 夹具：bare origin + 可克隆工作区 ──
function makeOrigin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fxv-origin-'));
  const bare = path.join(dir, 'origin.git');
  fs.mkdirSync(bare);
  git(bare, 'init', '--bare', '-b', 'main');
  const seed = path.join(dir, 'seed');
  fs.mkdirSync(seed);
  git(seed, 'init', '-b', 'main');
  fs.writeFileSync(path.join(seed, 'app.py'), 'def f(x):\n    return eval(x)  # RCE 隐患：修复目标\n');
  fs.writeFileSync(path.join(seed, 'test_app.py'), 'import app\n\ndef test_ok():\n    assert app.f("1+1") == 2\n');
  git(seed, 'add', '.'); git(seed, 'commit', '-m', 'seed');
  git(seed, 'push', bare.replace(/\\/g, '/'), 'main');
  return { dir, bareUrl: bare.replace(/\\/g, '/'), seedHead: git(seed, 'rev-parse', 'HEAD').trim() };
}

async function main() {
  await startPg();
  console.log(`PG: ${dsn.replace(/:[^:@/]+@/, ':****@')}`);
  const pool = new Pool({ connectionString: dsn });
  const store = await createFxvStore({ pool });
  await store.initSchema();

  const cfg = (over = {}) => loadFxvConfig({
    FXV_REPO_ALLOWLIST: 'acme/app', FXV_DRY_RUN: '1', ...process.env, ...over }).config;

  const filed = async (over = {}) => {
    const a = { attempt_id: 'att-' + sha().slice(0, 8), ticket_id: 'tkt-' + sha().slice(0, 8),
      finding_id: 'fn-' + sha().slice(0, 8), repo: 'acme/app', branch: 'main',
      base_head_sha: over.base_head ?? sha(), patch_digest: over.digest ?? digestOf('patch'), actor: 'itest', ...over };
    const r = await store.fileAttempt(a);
    return r.attempt;
  };
  const approve = async (id) => {
    await transition(store, { attemptId: id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
    await transition(store, { attemptId: id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  };
  const handlersFor = (origin, opts = {}) => {
    let lastWorkspace = null;
    return {
      generatePatch: async (cur) => ({ patch_text: opts.patchText ?? '+ eval(x) -> int(x)' + String.fromCharCode(10), patch_digest: opts.produceDigest ?? cur.patch_digest }),
      dryRunApply: async (cur) => {
        const ws = path.join(os.tmpdir(), 'fxv-ws-' + sha().slice(0, 8));
        fs.mkdirSync(ws);
        git(ws, 'init', '-b', 'main');
        git(ws, 'fetch', origin.bareUrl, cur.base_head_sha);
        git(ws, 'checkout', 'FETCH_HEAD');
        const fixed = fs.readFileSync(path.join(ws, 'app.py'), 'utf8').replace('eval(x)', 'int(x)');
        fs.writeFileSync(path.join(ws, 'app.py'), fixed);
        lastWorkspace = ws;
        return { applied: true, workspace: ws };
      },
      runTests: async () => {
        if (!lastWorkspace) return { passed: false, output: 'no workspace' };
        if (opts.postPushFail && opts.isPostPush?.()) return { passed: false, output: 'post-push verification failed (scenario)' };
        if (opts.testFails && !opts.isPostPush) return { passed: false, output: 'dry-run test failure (scenario)' };
        const body = fs.readFileSync(path.join(lastWorkspace, 'app.py'), 'utf8');
        return { passed: body.includes('int(x)') && !body.includes('eval(x)'), output: body.slice(0, 200) };
      },
      ...(opts.commitPush ? { commitPush: opts.commitPush } : {}),
      ...(opts.rollback ? { rollback: opts.rollback } : {}),
    };
  };

  console.log('== S1 成功链（dry-run，真 PG 状态机 + 真 git 应用）==');
  {
    const origin = makeOrigin();
    const a = await filed({ base_head: origin.seedHead, digest: digestOf('p1') });
    await approve(a.attempt_id);
    const end = await runPipeline(store, cfg(), handlersFor(origin), a.attempt_id);
    ok('S1 dry-run 全链 → DRY_RUN_COMPLETE', end === STATES.DRY_RUN_COMPLETE, `end=${end}`);
    const evs = await store.listEvents(a.attempt_id);
    ok('S1 审计链完整（FILED→…→DRY_RUN_COMPLETE）', ['FILED', 'TRANSITION'].every((k) => evs.some((e) => e.kind === k)) && evs.some((e) => e.to_state === STATES.DRY_RUN_VERIFIED));
    ok('S1 幂等重放不产生新副作用', (await store.fileAttempt({ ...a, attempt_id: 'att-x', ticket_id: a.ticket_id })).created === false);
  }

  console.log('== S2 finding/attempt 不存在 ==');
  {
    try {
      await transition(store, { attemptId: 'att-nope', from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
      ok('S2 未知 attempt → TransitionConflict', false);
    } catch (e) {
      ok('S2 未知 attempt → TransitionConflict', e instanceof TransitionConflict && e.message === 'attempt_not_found');
    }
  }

  console.log('== S3 stale head ==');
  {
    const a = await filed();
    await approve(a.attempt_id);
    try {
      await transition(store, { attemptId: a.attempt_id, from: STATES.APPROVED, to: STATES.PATCH_GENERATING, meta: { head_sha: sha() } });
      ok('S3 head 漂移转迁被拒', false);
    } catch (e) {
      ok('S3 head 漂移 → STALE_HEAD 终态', e.message === 'stale_head' && (await store.getAttempt(a.attempt_id)).state === STATES.STALE_HEAD);
    }
  }

  console.log('== S4 wrong repo（allowlist）==');
  {
    const a = await filed({ repo: 'other/repo' });
    await approve(a.attempt_id);
    const end = await runPipeline(store, cfg(), {}, a.attempt_id);
    const row = await store.getAttempt(a.attempt_id);
    ok('S4 越权仓库 → ERROR_FATAL + 审计原因', end === STATES.ERROR_FATAL && row.state_detail.last_reason === 'REPO_NOT_IN_FXV_ALLOWLIST', `end=${end} ${row.state_detail.last_reason}`);
  }

  console.log('== S5 patch digest drift ==');
  {
    const origin = makeOrigin();
    const a = await filed({ base_head: origin.seedHead, digest: digestOf('will-not-match') });
    await approve(a.attempt_id);
    const end = await runPipeline(store, cfg(), handlersFor(origin, { produceDigest: digestOf('actual-different-digest') }), a.attempt_id);
    const row = await store.getAttempt(a.attempt_id);
    ok('S5 生成补丁摘要与立案不符 → DIGEST_DRIFT', end === STATES.ERROR_FATAL && /digest drift/.test(row.state_detail.last_reason), `end=${end} ${row.state_detail.last_reason}`);
  }

  console.log('== S6 测试失败 ==');
  {
    const origin = makeOrigin();
    const a = await filed({ base_head: origin.seedHead, digest: digestOf('p6') });
    await approve(a.attempt_id);
    const end = await runPipeline(store, cfg(), handlersFor(origin, { testFails: true }), a.attempt_id);
    ok('S6 隔离测试失败 → TEST_FAILED', end === STATES.TEST_FAILED, `end=${end}`);
  }

  console.log('== S7 并发 claim（两执行者竞争同一转迁）==');
  {
    const a = await filed();
    const t = (actor) => transition(store, { attemptId: a.attempt_id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL, actor });
    const rs = await Promise.allSettled([t('runner-a'), t('runner-b')]);
    const wins = rs.filter((r) => r.status === 'fulfilled' && !r.value.idempotent);
    const idem = rs.filter((r) => r.status === 'fulfilled' && r.value.idempotent);
    ok('S7 恰好一个非幂等赢家', wins.length === 1 && idem.length === 1, JSON.stringify(rs.map((r) => r.status)));
  }

  console.log('== S8 重启恢复（新 store 实例 recover）==');
  {
    const origin = makeOrigin();
    const aExp = await filed({ ticket_id: 'tkt-exp-1' });
    const aStale = await filed({ ticket_id: 'tkt-stale-1' });
    const aOk = await filed({ base_head: origin.seedHead, digest: digestOf('p8') });
    await approve(aOk.attempt_id);
    await pool.query("UPDATE fxv.attempts SET expires_at = now() - interval '1 second' WHERE attempt_id=$1", [aExp.attempt_id]);
    const store2 = await createFxvStore({ pool }); // 模拟重启后的新实例
    const report = await recover(store2, cfg(), { currentHead: async (a) => a.attempt_id === aStale.attempt_id ? sha() : a.base_head_sha });
    ok('S8 过期 → EXPIRED', (await store2.getAttempt(aExp.attempt_id)).state === STATES.EXPIRED);
    ok('S8 head 漂移 → STALE_HEAD', (await store2.getAttempt(aStale.attempt_id)).state === STATES.STALE_HEAD);
    ok('S8 可续跑保持 APPROVED', (await store2.getAttempt(aOk.attempt_id)).state === STATES.APPROVED);
    const end = await runPipeline(store2, cfg(), handlersFor(origin), aOk.attempt_id);
    ok('S8 续跑至 DRY_RUN_COMPLETE', end === STATES.DRY_RUN_COMPLETE, `end=${end}`);
  }

  console.log('== S9 rollback（真实提交本地 bare 后测试失败 → 回滚）==');
  {
    const origin = makeOrigin();
    const a = await filed({ base_head: origin.seedHead, digest: digestOf('p9') });
    await approve(a.attempt_id);
    await store.issueGrant({ grant_id: 'g-' + sha().slice(0, 6), operator: 'ngh', repo: 'acme/app', ttl_ms: 60000 });
    const cfgReal = cfg({ FXV_DRY_RUN: '0', FXV_GITHUB_WRITE: 'authorized' });
    let pushedSha = null;
    const ws = path.join(os.tmpdir(), 'fxv-push-' + sha().slice(0, 8));
    const commitPush = async (cur, gate) => {
      await gate.assert('acme/app');
      fs.mkdirSync(ws);
      void cur;
      git(ws, 'init', '-b', 'main');
      git(ws, 'fetch', origin.bareUrl, cur.base_head_sha);
      git(ws, 'checkout', 'FETCH_HEAD');
      fs.writeFileSync(path.join(ws, 'app.py'), fs.readFileSync(path.join(ws, 'app.py'), 'utf8').replace('eval(x)', 'int(x)'));
      git(ws, 'add', '.'); git(ws, 'commit', '-m', 'fix: eval→int (fxv attempt)');
      git(ws, 'push', origin.bareUrl, 'HEAD:refs/heads/main');
      pushedSha = git(ws, 'rev-parse', 'HEAD').trim();
      return { committed_sha: pushedSha };
    };
    const rollback = async (cur) => {
      void cur;
      git(ws, 'push', origin.bareUrl, '--force', `${origin.seedHead}:main`);
    };
    const end = await runPipeline(store, cfgReal, handlersFor(origin, {
      postPushFail: true, isPostPush: () => pushedSha !== null, commitPush, rollback,
    }), a.attempt_id);
    const row = await store.getAttempt(a.attempt_id);
    ok('S9 推送后失败 → ROLLED_BACK 且 bare 回到 base head', end === STATES.ROLLED_BACK, `end=${end} detail=${row.state_detail.last_reason}`);
    const headNow = git(origin.bareUrl.replace('/origin.git', ''), '--git-dir', origin.bareUrl, 'rev-parse', 'main').trim();
    ok('S9 bare main == seed head（回滚成功）', headNow === origin.seedHead, `${headNow} vs ${origin.seedHead}`);
  }

  console.log('== S10 补丁含凭据形状 → 拒绝 ==');
  {
    const origin = makeOrigin();
    const badToken = 'ghp_' + 'Qw7Zx9Km'.repeat(5);
    const a = await filed({ base_head: origin.seedHead, digest: digestOf('p10') });
    await approve(a.attempt_id);
    const end = await runPipeline(store, cfg(), handlersFor(origin, { patchText: `+ token = "${badToken}"\n` .replace('\n .replace','') }), a.attempt_id);
    const row = await store.getAttempt(a.attempt_id);
    ok('S10 凭据形状补丁 → ERROR_FATAL(secret-shaped)', end === STATES.ERROR_FATAL && /secret-shaped/.test(row.state_detail.last_reason), row.state_detail.last_reason);
  }

  console.log('== S11 GitHub 写边界（未授权禁止真实写）==');
  {
    const origin = makeOrigin();
    const a = await filed({ base_head: origin.seedHead, digest: digestOf('p11') });
    await approve(a.attempt_id);
    const end = await runPipeline(store, cfg({ FXV_DRY_RUN: '0' }), handlersFor(origin, {
      commitPush: async () => { throw new Error('S11: must not reach commitPush without grant'); },
    }), a.attempt_id);
    ok('S11 dry_run=off 且无授权 → AWAITING_GITHUB_GRANT（真实写不可达）', end === STATES.AWAITING_GITHUB_GRANT, `end=${end}`);
    const g = await consumePipelineGrant(cfg({ FXV_DRY_RUN: '0' }), store, 'acme/app');
    ok('S11 disabled 模式即使存在可用授权也不消费', g === null, `g=${g?.grant_id}`);
  }

  console.log(`\nfxv-pg integration: ${pass} passed, ${fail} failed`);
  await pool.end();
  await stopPg();
  process.exit(fail > 0 ? 1 : 0);
}

main().catch(async (e) => { console.error('FATAL', e); await stopPg(); process.exit(1); });
