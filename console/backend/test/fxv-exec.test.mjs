// console/backend/test/fxv-exec.test.mjs — 真实执行栈子进程回路（fixer/verifier 独立进程）。
// 运行：node --test console/backend/test/fxv-exec.test.mjs（需 git；仓库=本地 bare，零 GitHub）
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { makeExecHandlers, ruleDigest } from '../lib/fxv/exec/exec.mjs';
import { transition, runPipeline, STATES } from '../lib/fxv/orchestrator.mjs';
import { createFxvStore } from '../lib/fxv/store.mjs';
import crypto from 'node:crypto';

const git = (cwd, ...a) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', ...a], { cwd, encoding: 'utf8' });
const sha = () => crypto.randomBytes(10).toString('hex');

function origin() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fxvexec-'));
  const bare = path.join(dir, 'o.git'); fs.mkdirSync(bare);
  git(bare, 'init', '--bare', '-b', 'main');
  const seed = path.join(dir, 's'); fs.mkdirSync(seed);
  git(seed, 'init', '-b', 'main');
  fs.writeFileSync(path.join(seed, 'app.py'), 'def f(x):\n    return eval(x)\n');
  git(seed, 'add', '.'); git(seed, 'commit', '-m', 'seed'); git(seed, 'push', bare.replace(/\\/g, '/'), 'main');
  return { url: bare.replace(/\\/g, '/'), head: git(seed, 'rev-parse', 'HEAD').trim() };
}

const memStore = () => ({
  m: new Map(),
  async initSchema() {},
  async fileAttempt(a) { if (this.m.has(a.ticket_id)) return { created: false, attempt: this.m.get(a.ticket_id) };
    const row = { ...a, state: 'FILED', state_detail: { rule_file: a.rule_file, rule_pattern: a.rule_pattern, rule_replacement: a.rule_replacement }, attempts_count: 0, expires_at: new Date(Date.now() + 86400_000).toISOString() };
    this.m.set(a.attempt_id, row); return { created: true, attempt: row }; },
  async getAttempt(i) { return this.m.get(i) ?? null; },
  async getAttemptByTicket(t) { for (const [, r] of this.m) if (r.ticket_id === t) return r; return null; },
  async listNonTerminal(term) { return [...this.m.values()].filter((r) => !term.includes(r.state)); },
  async compareAndSetState(i, e, n, d) { const r = this.m.get(i); if (!r || r.state !== e) return null;
    r.state = n; r.state_detail = { ...(r.state_detail ?? {}), ...(d ?? {}) }; return { ...r }; },
  async recordEvent() {}, async listEvents() { return []; },
  async issueGrant() { return null; }, async consumeGrant() { return null; }, async activeGrantCount() { return 0; },
});

const TEST_OK = ['node', '-e', "const s=require('fs').readFileSync('app.py','utf8');if(!s.includes('int(x)')||s.includes('eval(x)'))process.exit(1)"];
const cfg = () => ({ repoAllowlist: ['acme/app'], branchAllowlist: ['*'], dryRun: true, githubWrite: 'disabled',
  timeouts: { step_ms: 30_000, approval_ttl_ms: 86400_000, grant_ttl_ms: 3600_000 }, maxRetries: 1 });

async function filedExec(store, o, rule = {}) {
  const r = { file: 'app.py', pattern: 'eval(x)', replacement: 'int(x)', ...rule };
  const res = await store.fileAttempt({ attempt_id: 'att-' + sha(), ticket_id: 'tkt-' + sha(),
    finding_id: 'fn-' + sha(), repo: 'acme/app', branch: 'main', base_head_sha: o.head,
    patch_digest: ruleDigest(o.head, r.file, r.pattern, r.replacement), actor: 't',
    rule_file: r.file, rule_pattern: r.pattern, rule_replacement: r.replacement });
  const id = res.attempt.attempt_id;
  await transition(store, { attemptId: id, from: STATES.FILED, to: STATES.AWAITING_APPROVAL });
  await transition(store, { attemptId: id, from: STATES.AWAITING_APPROVAL, to: STATES.APPROVED });
  return store.getAttempt(id);
}

test('exec 全链：真 fixer+verifier 子进程 → DRY_RUN_COMPLETE（digest=rule@head 公式一致）', async () => {
  const o = origin(); const store = memStore();
  const a = await filedExec(store, o);
  const h = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url, testCmd: TEST_OK });
  const end = await runPipeline(store, cfg(), h, a.attempt_id);
  assert.equal(end, STATES.DRY_RUN_COMPLETE, `end=${end} detail=${JSON.stringify((await store.getAttempt(a.attempt_id)).state_detail)}`);
});

test('exec empty patch：pattern==replacement → PATCH_EMPTY → ERROR_FATAL', async () => {
  const o = origin(); const store = memStore();
  const a = await filedExec(store, o, { pattern: 'eval(x)', replacement: 'eval(x)' });
  const h = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url, testCmd: TEST_OK });
  const end = await runPipeline(store, cfg(), h, a.attempt_id);
  const row = await store.getAttempt(a.attempt_id);
  assert.equal(end, STATES.ERROR_FATAL);
  assert.match(row.state_detail.last_reason, /EMPTY_PATCH|patch is empty/);
});

test('exec pattern 不在 head 上 → PATTERN_NOT_FOUND_AT_HEAD → ERROR_FATAL', async () => {
  const o = origin(); const store = memStore();
  const a = await filedExec(store, o, { pattern: 'not-in-code', replacement: 'x' });
  const h = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url, testCmd: TEST_OK });
  const end = await runPipeline(store, cfg(), h, a.attempt_id);
  assert.equal(end, STATES.ERROR_FATAL);
  assert.match((await store.getAttempt(a.attempt_id)).state_detail.last_reason, /PATTERN_NOT_FOUND/);
});

test('exec verifier 独立拒绝：digest 绑定不符（rule 改写后立案摘要仍旧）→ DIGEST_DRIFT', async () => {
  const o = origin(); const store = memStore();
  const a = await filedExec(store, o, { pattern: 'eval(x)', replacement: 'str(x)' }); // 立案 str(x)
  // 篡改立案 digest 模拟漂移：直接断言公式校验路径——handler 产出与立案不符
  const row = store.m.get(a.attempt_id);
  row.patch_digest = ruleDigest(o.head, 'app.py', 'eval(x)', 'int(x)'); // 不同 rule 的摘要
  const h = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url, testCmd: TEST_OK });
  const end = await runPipeline(store, cfg(), h, a.attempt_id);
  const r2 = await store.getAttempt(a.attempt_id);
  assert.equal(end, STATES.ERROR_FATAL);
  assert.match(r2.state_detail.last_reason, /digest drift/);
});

test('exec harness 失败（真子进程退出码）→ TEST_FAILED', async () => {
  const o = origin(); const store = memStore();
  const a = await filedExec(store, o);
  const h = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url,
    testCmd: ['node', '-e', 'process.exit(1)'] });
  const end = await runPipeline(store, cfg(), h, a.attempt_id);
  assert.equal(end, STATES.TEST_FAILED);
});

test('ruleDigest 公式与 fixer 产出一致（同一 head/rule）', async () => {
  const o = origin(); const store = memStore();
  const a = await filedExec(store, o);
  const h = makeExecHandlers({ cfg: cfg(), repoUrl: () => o.url, testCmd: TEST_OK });
  const out = await h.generatePatch(store.m.get(a.attempt_id));
  assert.equal(out.patch_digest, ruleDigest(o.head, 'app.py', 'eval(x)', 'int(x)'));
  assert.match(out.patch_text, /int\(x\)/);
});

// PG store 兼容冒烟（fileAttemptFromFinding 在 staging smoke 全量覆盖）
test('createFxvStore 存在性（PG 路径由 staging smoke 覆盖）', async () => {
  assert.equal(typeof createFxvStore, 'function');
});
