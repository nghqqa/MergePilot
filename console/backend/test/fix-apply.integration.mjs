// fix-apply.integration.mjs — 应用修复/合并 PR 波集成测试（本地 git 物化 + PG + fetch mock）。
// 覆盖：materializePatchedFiles（隔离副本真 apply+全文件内容重建）、远端写入原语
//（Gitee blob→tree→commit→ref 条件链 + 条件合并）、服务层门（confirm/幂等/head 复核/
// 已应用防重）、操作记录落库（mu.fix_apply_operation）。
// 纪律：全部远端交互走 fetch mock——CI 零真实外呼；真实双平台写入验收在候选部署阶段执行。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Z]:)/, '$1'));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass += 1; console.log('  PASS ', name); }
  else { fail += 1; console.log('  FAIL ', name, JSON.stringify(detail ?? '').slice(0, 300)); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fa = await import('../lib/multiuser/fix-apply.mjs');
const rw = await import('../lib/multiuser/remote-write.mjs');
const art = await import('../lib/multiuser/artifacts.mjs');

const USER = crypto.randomUUID();
const HEAD = 'abbfe8a157172ef4f96a2437bf2114374b9fa378';
const HEAD2 = 'bccfe8a157172ef4f96a2437bf2114374b9fa379';
const GOOD_DIFF = [
  '--- a/src/config.js', '+++ b/src/config.js', '@@ -1,3 +1,4 @@',
  ' const x = 1;', '-const token = "ghp_x";', '+const token = process.env.TOKEN;', '+const y = 2;',
].join('\n');

const jsonResp = (obj, status = 200) => ({ ok: status >= 200 && status < 300, status,
  json: async () => obj, text: async () => JSON.stringify(obj) });

// ── L 组：本地 git 物化（无网络）──
{
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-mat-t-'));
  const g = (args) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', ...args],
    { cwd: dir, stdio: 'pipe' });
  g(['init', '-q']);
  execFileSync('git', ['config', 'core.autocrlf', 'false'], { cwd: dir, stdio: 'pipe' });
  fs.writeFileSync(path.join(dir, 'app.js'), 'const a = 1;\nconst b = 2;\nconst c = 3;\n');
  g(['add', '.']); g(['commit', '-qm', 'v1']);
  const head1 = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir }).toString().trim();
  fs.writeFileSync(path.join(dir, 'app.js'), 'const a = 1;\nconst b = 42;\nconst c = 3;\nconst d = 4;\n');
  g(['add', '.']); g(['commit', '-qm', 'v2']);
  const head2 = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir }).toString().trim();
  const diff = execFileSync('git', ['diff', head1, head2], { cwd: dir }).toString();

  const r1 = await (await import('../lib/multiuser/patch-applicability.mjs'))
    .materializePatchedFiles({ repoUrl: dir, headSha: head1, diffText: diff });
  ok('L1 物化：隔离副本真 apply + 全文件内容重建',
    r1.ok === true && r1.files?.length === 1
    && r1.files[0].content === 'const a = 1;\nconst b = 42;\nconst c = 3;\nconst d = 4;\n', r1);

  // 补丁与 head 不匹配（对 v2 head 应用 v1→v2 的 diff = 上下文错位）→ 失败且零副作用
  const r2 = await (await import('../lib/multiuser/patch-applicability.mjs'))
    .materializePatchedFiles({ repoUrl: dir, headSha: head2, diffText: diff });
  ok('L2 物化：head 不匹配 → patch_apply_failed', r2.ok === false && r2.reason === 'patch_apply_failed', r2);

  // 删除文件物化 → content=null（供 tree 删除条目）
  fs.writeFileSync(path.join(dir, 'gone.txt'), 'x\n');
  g(['add', '.']); g(['commit', '-qm', 'v3']);
  const head3 = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir }).toString().trim();
  fs.rmSync(path.join(dir, 'gone.txt'));
  g(['add', '.']); g(['commit', '-qm', 'v4']);
  const head4 = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: dir }).toString().trim();
  const r3 = await (await import('../lib/multiuser/patch-applicability.mjs'))
    .materializePatchedFiles({ repoUrl: dir, headSha: head3,
      diffText: execFileSync('git', ['diff', head3, head4], { cwd: dir }).toString() });
  ok('L3 物化：删除文件 → content=null 条目',
    r3.ok === true && r3.files?.some((f) => f.path === 'gone.txt' && f.content === null), r3);
  fs.rmSync(dir, { recursive: true, force: true });
}

// ── R 组：远端写入原语（本地裸仓=真 push 语义，零网络 mock）──
{
  const env = { MU_GITEE_PAT: 'testpat' };
  // 建源仓（含 feature/pilot-x 形态的斜杠分支）+ 裸仓作远端
  const src = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-push-src-'));
  const bare = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-push-bare-')) + '/remote.git';
  const gp = (cwd, args) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', ...args],
    { cwd, stdio: 'pipe' }).toString();
  gp(src, ['init', '-q', '-b', 'master']);
  execFileSync('git', ['config', 'core.autocrlf', 'false'], { cwd: src, stdio: 'pipe' });
  fs.writeFileSync(path.join(src, 'src.txt'), 'line1\nline2\nline3\n');
  gp(src, ['add', '.']); gp(src, ['commit', '-qm', 'v1']);
  gp(src, ['remote', 'add', 'origin', bare]);
  gp(bare.replace('/remote.git', ''), ['init', '-q', '--bare', 'remote.git']);
  gp(src, ['push', '-q', 'origin', 'master:refs/heads/feature/pilot-x']);
  const base = gp(src, ['rev-parse', 'master']).trim();

  // R1: 推送式提交——happy path（斜杠分支 + patch 全内容变更）
  const patchTxt = [
    '--- a/src.txt', '+++ b/src.txt', '@@ -1,3 +1,3 @@',
    ' line1', '-line2', '+line2-fixed', ' line3',
  ].join('\n') + '\n';
  const c1 = await rw.platformCreateCommit({ env, provider: 'gitee', owner: 'o', repo: 'r',
    repoUrl: bare, branch: 'feature/pilot-x', expectedHead: base, diffText: patchTxt, message: 'fix: r1' });
  ok('R1 推送式提交：真 push 到斜杠分支（fast-forward 成功）',
    c1.ok === true && /^[0-9a-f]{40}$/.test(c1.commitSha) && c1.previousSha === base, c1);
  // 远端实证：分支 tip=新提交；提交作者=MergePilot Bot
  const tipOut = execFileSync('git', ['--git-dir', bare, 'rev-parse', 'refs/heads/feature/pilot-x']).toString().trim();
  const authorOut = execFileSync('git', ['--git-dir', bare, 'log', '-1', '--format=%an %ae', 'refs/heads/feature/pilot-x']).toString().trim();
  ok('R2 远端 tip=新提交 + 作者=MergePilot Bot', tipOut === c1.commitSha && authorOut === 'MergePilot Bot bot@mergepilot.local', { tipOut, authorOut });

  // R3: 陈旧 head → 拒绝（ls-remote 前检；零 push）
  const c2 = await rw.platformCreateCommit({ env, provider: 'gitee', owner: 'o', repo: 'r',
    repoUrl: bare, branch: 'feature/pilot-x', expectedHead: base, diffText: patchTxt, message: 'fix: r3' });
  ok('R3 前置 CAS：expectedHead 已过期 → stale_head 零写入',
    c2.ok === false && c2.reason === 'stale_head', c2);

  // R4: 服务端 CAS——基于旧 head 的 push 被拒（先制造并发推进）
  const staleBase = base;
  const patchOld = [
    '--- a/src.txt', '+++ b/src.txt', '@@ -1,3 +1,3 @@',
    ' line1', '-line2', '+line2-old', ' line3',
  ].join('\n');
  // 构造并发推进：另一副本基于当前 feature 分支 tip 推一个新提交，使 staleBase 过期
  const forkDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-push-fork-'));
  execFileSync('git', ['clone', '-q', bare, forkDir], { stdio: 'pipe' });
  execFileSync('git', ['config', 'core.autocrlf', 'false'], { cwd: forkDir, stdio: 'pipe' });
  execFileSync('git', ['checkout', '-q', '-B', 'feature/pilot-x', 'origin/feature/pilot-x'], { cwd: forkDir, stdio: 'pipe' });
  fs.writeFileSync(path.join(forkDir, 'src.txt'), 'line1\nline2-fixed\nline3-concurrent\n');
  gp(forkDir, ['add', '.']); gp(forkDir, ['commit', '-qm', 'concurrent']);
  gp(forkDir, ['push', '-q', 'origin', 'HEAD:refs/heads/feature/pilot-x']);
  const newTip = gp(forkDir, ['rev-parse', 'HEAD']).trim();
  // 此时 staleBase 已过期——pushCommit 的 ls-remote 前检应拒绝（服务端 CAS 不必触达）
  const c4 = await rw.platformCreateCommit({ env, provider: 'gitee', owner: 'o', repo: 'r',
    repoUrl: bare, branch: 'feature/pilot-x', expectedHead: staleBase, diffText: patchOld, message: 'fix: r4' });
  ok('R4 服务端推进后 → stale_head（条件更新防覆盖他人提交）',
    c4.ok === false && c4.reason === 'stale_head', c4);

  // R5: 分支不存在 → branch_not_found
  const c5 = await rw.platformCreateCommit({ env, provider: 'gitee', owner: 'o', repo: 'r',
    repoUrl: bare, branch: 'no/such-branch', expectedHead: newTip, diffText: patchTxt, message: 'fix: r5' });
  ok('R5 分支不存在 → branch_not_found', c5.ok === false && c5.reason === 'branch_not_found', c5);

  // R6: 补丁不适用 → patch_apply_failed（零写入）
  const c6 = await rw.platformCreateCommit({ env, provider: 'gitee', owner: 'o', repo: 'r',
    repoUrl: bare, branch: 'feature/pilot-x', expectedHead: newTip, diffText: GOOD_DIFF, message: 'fix: r6' });
  ok('R6 补丁不适用 → patch_apply_failed 零写入',
    c6.ok === false && c6.reason === 'patch_apply_failed', c6);

  // R7/R8: 条件合并（fetch mock——Gitee PUT merge / GitHub merge API）
  const calls = [];
  const realFetchR = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url).split('?')[0]; const m = opts.method || 'GET';
    calls.push(`${m} ${u}`);
    if (/\/pulls\/2$/.test(u) && m === 'GET') return jsonResp({ state: 'open', head: { sha: HEAD, ref: 'fix' } });
    if (/\/pulls\/2\/merge$/.test(u) && m === 'PUT') return jsonResp({ sha: 'mergesha' }, 200);
    return jsonResp({}, 404);
  };
  const c7 = await rw.giteeMergePR({ env, owner: 'o', repo: 'r', prNumber: 2,
    expectedHead: HEAD2, mergeMethod: 'merge' });
  ok('R7 条件合并：head 不匹配 → stale_head', c7.ok === false && c7.reason === 'stale_head', c7);
  const c8 = await rw.giteeMergePR({ env, owner: 'o', repo: 'r', prNumber: 2,
    expectedHead: HEAD, mergeMethod: 'merge', commitTitle: 'Merge PR #2' });
  ok('R8 条件合并：head 匹配 → merged（PUT 恰一次）',
    c8.ok === true && c8.mergeSha === 'mergesha'
    && calls.filter((c) => c.startsWith('PUT') && c.includes('/merge')).length === 1, c8);
  globalThis.fetch = realFetchR;

  // R9: GitHub JWT 无 require 残留（ESM 兼容回归）
  let ghErr = '';
  try {
    await rw.githubMergePR({ env: { MU_GITHUB_APP_ID: '1', MU_GITHUB_APP_PRIVATE_KEY: 'not-a-key' },
      installationId: '9', owner: 'o', repo: 'r', prNumber: 2, expectedHead: HEAD });
  } catch (e) { ghErr = String(e?.message ?? e); }
  ok('R9 GitHub JWT 无 require 残留（ESM 兼容）', !/require is not defined/.test(ghErr), ghErr);

  for (const d of [src, forkDir, bare.replace('/remote.git', '')]) {
    try { fs.rmSync(d, { recursive: true, force: true }); } catch { /* 尽力 */ }
  }
}

// ── PG 组：服务层门 + 操作记录 ──
const RUN = crypto.randomUUID();
const TEN = crypto.randomUUID();
const REPO = crypto.randomUUID();
const PRID = crypto.randomUUID();
const CTR = `mu-fixap-${crypto.randomBytes(3).toString('hex')}`;
const PORT = 17700 + Math.floor(Math.random() * 60);

try {
  execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
  for (let i = 0; i < 150; i++) { try { await pool.query('SELECT 1'); break; } catch { await sleep(400); } }
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();
  const T = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;

  await pool.query(
    `INSERT INTO mu.repository (repo_id, tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,$2,'gitee','50740631','nghqqa','mergepilot-test')`, [REPO, T]);
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,2,$4,'open')`, [PRID, T, REPO, HEAD]);
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUN, T, REPO, PRID, HEAD]);
  const pSeed = await art.persistFixPatch(pool, { runId: RUN, tenantId: T, repoId: REPO,
    headSha: HEAD, diffText: GOOD_DIFF, rationale: 'move token to env',
    changedFiles: [{ path: 'src/config.js' }] });
  ok('P0 种子补丁入库', pSeed.ok === true && pSeed.patchId, pSeed);

  // fetch mock：PR 读取 + 平台写入（gitee）
  const remoteState = { prHead: HEAD, prState: 'open', mergeable: true, refSha: HEAD };
  const platformCalls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url).split('?')[0]; const m = opts.method || 'GET';
    platformCalls.push(`${m} ${u}`);
    if (/\/pulls\/2$/.test(u) && m === 'GET') {
      return jsonResp({ state: remoteState.prState, head: { sha: remoteState.prHead, ref: 'fix-branch' },
        base: { ref: 'master' }, mergeable: remoteState.mergeable });
    }
    if (/\/git\/refs\/heads\//.test(u) && m === 'GET') return jsonResp({ object: { sha: remoteState.refSha } });
    if (/\/git\/blobs/.test(u)) return jsonResp({ sha: `blob-${platformCalls.length}` }, 201);
    if (/\/git\/commits\/[^/]+$/.test(u) && m === 'GET') return jsonResp({ tree: { sha: 'basetree' } });
    if (/\/git\/trees$/.test(u)) return jsonResp({ sha: 'newtree' }, 201);
    if (/\/git\/commits$/.test(u) && m === 'POST') return jsonResp({ sha: 'newcommit' }, 201);
    if (/\/git\/refs\/heads\//.test(u) && m === 'PATCH') return jsonResp({ object: { sha: 'newcommit' } });
    if (/\/pulls\/2\/merge$/.test(u) && m === 'PUT') return jsonResp({ sha: 'mergesha' });
    return jsonResp({}, 404);
  };
  // 物化禁止触网：confirm 路径的 git fetch 指向不可达本地端口（快速失败=materialize_failed 分支）
  const env = { ...process.env, MU_GITEE_PAT: 'testpat', MU_GITEE_INSTANCE_WEB_BASE: 'http://127.0.0.1:9' };

  // ── preview：apply ──
  const v1 = await fa.previewApplyFix(pool, { env, runId: RUN, tenantId: T });
  ok('P1 预览：head 一致 → can_apply + 机器人身份（Gitee Bot）',
    v1.ok === true && v1.can_apply === true && v1.robot_identity === 'MergePilot Gitee Bot'
    && v1.target.head_match === true && v1.patch.applicability_state === 'not_executed', v1);
  const v2 = await fa.previewApplyFix(pool, { env, runId: crypto.randomUUID(), tenantId: T });
  ok('P2 预览：run 不存在 → run_not_found', v2.ok === false && v2.reason === 'run_not_found', v2);

  remoteState.prHead = HEAD2;
  const v3 = await fa.previewApplyFix(pool, { env, runId: RUN, tenantId: T });
  ok('P3 预览：head 已推进 → 拒绝并给用户可读原因',
    v3.ok === true && v3.can_apply === false && v3.blocked_reason === 'head_moved', v3.blocked_reason);
  remoteState.prHead = HEAD;

  // ── confirm：apply ──
  const x1 = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, confirm: false });
  ok('P4 确认门：缺 confirm → confirm_required 零副作用',
    x1.ok === false && x1.reason === 'confirm_required'
    && (await pool.query(`SELECT count(*)::int c FROM mu.fix_apply_operation`)).rows[0].c === 0, x1);

  remoteState.prHead = HEAD2;
  const x2 = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER, confirm: true });
  const op2 = (await pool.query(
    `SELECT status, error_code FROM mu.fix_apply_operation WHERE run_id=$1`, [RUN])).rows[0];
  ok('P5 陈旧 head：确认被拒 + 操作记录 failed + 零平台写',
    x2.ok === false && x2.reason === 'stale_head' && op2?.status === 'failed' && op2?.error_code === 'stale_head'
    && !platformCalls.some((c) => c.includes('/git/blobs')), { x2, op2 });
  remoteState.prHead = HEAD;

  // 预览→确认窗口漂移（expectedRemoteHead 不一致）
  const x2b = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: null,
    userId: USER, confirm: true, expectedRemoteHead: '1111111111111111111111111111111111111111' });
  ok('P5b 预览后 head 漂移 → head_moved_since_preview',
    x2b.ok === false && x2b.reason === 'head_moved_since_preview', x2b);

  // 幂等：已 committed 的同键操作 → 直接返回既有结果
  const ID = pSeed.patchId;
  await pool.query(
    `UPDATE mu.fix_apply_operation SET status='committed',
       candidate_commit_sha='${'e'.repeat(40)}', remote_result_sha='${'e'.repeat(40)}'
      WHERE run_id=$1`, [RUN]);
  const before = platformCalls.length;
  const x3 = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER, confirm: true });
  ok('P6 幂等：同 run+digest 已提交 → 幂等返回且零新增平台调用',
    x3.ok === true && x3.idempotent === true && x3.commit_sha === 'e'.repeat(40)
    && platformCalls.length === before, { x3, delta: platformCalls.length - before });

  // 已应用后预览 → already_applied
  const v4 = await fa.previewApplyFix(pool, { env, runId: RUN, tenantId: T });
  ok('P7 已应用后预览 → blocked already_applied（防重复提交）',
    v4.can_apply === false && v4.blocked_reason === 'already_applied'
    && v4.previous_operations?.length >= 1, v4);

  // ── merge：preview + confirm ──
  const RUN2 = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUN2, T, REPO, PRID, HEAD2]);
  remoteState.prHead = HEAD2;
  const mg1 = await fa.previewMergePr(pool, { env, runId: RUN2, tenantId: T });
  ok('P8 合并预览：开放+无冲突 → can_merge + 条件可见化（CI 未接入如实标注）',
    mg1.ok === true && mg1.can_merge === true
    && mg1.merge_conditions.ci_status === 'not_configured'
    && mg1.merge_conditions.branch_protection === 'not_bypassed'
    && mg1.target.source_branch === 'fix-branch', mg1);

  remoteState.prState = 'closed';
  const mg2 = await fa.previewMergePr(pool, { env, runId: RUN2, tenantId: T });
  ok('P9 合并预览：PR 已关闭 → 拒绝', mg2.ok === true && mg2.can_merge === false
    && mg2.blocked_reason === 'pr_not_open', mg2);
  remoteState.prState = 'open';

  const mg3 = await fa.confirmMergePr(pool, { env, runId: RUN2, tenantId: T, userId: USER,
    confirm: true, expectedHead: HEAD });
  ok('P10 合并确认：预览后 head 漂移 → 拒绝零远端写',
    mg3.ok === false && mg3.reason === 'head_moved_since_preview'
    && !platformCalls.some((c) => c.includes('/merge') && c.startsWith('PUT')), mg3);

  const mg4 = await fa.confirmMergePr(pool, { env, runId: RUN2, tenantId: T, userId: USER,
    confirm: true, expectedHead: HEAD2, mergeMethod: 'merge' });
  const op4 = (await pool.query(
    `SELECT status, remote_result_sha, operation_type FROM mu.fix_apply_operation
      WHERE run_id=$1 AND operation_type='merge_pr'`, [RUN2])).rows[0];
  ok('P11 合并确认：条件合并成功 + 记录 merged',
    mg4.ok === true && mg4.status === 'merged' && mg4.merge_sha === 'mergesha'
    && op4?.status === 'merged' && op4?.remote_result_sha === 'mergesha', { mg4, op4 });

  const mg5 = await fa.confirmMergePr(pool, { env, runId: RUN2, tenantId: T, userId: USER,
    confirm: true, expectedHead: HEAD2 });
  ok('P12 合并幂等：同 head 重复确认 → 幂等返回',
    mg5.ok === true && mg5.idempotent === true, mg5);

  globalThis.fetch = realFetch;
  await pool.end();
  console.log(`\nfix-apply.integration: ${pass} pass / ${fail} fail`);
  process.exit(fail ? 1 : 0);
} catch (e) {
  console.error('HARNESS ERROR', e);
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* 尽力 */ }
  process.exit(1);
} finally {
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* 尽力 */ }
}
