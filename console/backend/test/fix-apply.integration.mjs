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
const pa = await import('../lib/multiuser/patch-applicability.mjs');

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
  ].join('\n') + '\n';
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

  // R5: 分支不存在 → branch_not_found（新文件补丁在任何 head 均可应用，失败点在 push 阶段分支检查）
  const newFilePatch = ['diff --git a/probe.txt b/probe.txt', 'new file mode 100644',
    '--- /dev/null', '+++ b/probe.txt', '@@ -0,0 +1 @@', '+probe'].join('\n') + '\n';
  const c5 = await rw.platformCreateCommit({ env, provider: 'gitee', owner: 'o', repo: 'r',
    repoUrl: bare, branch: 'no/such-branch', expectedHead: newTip, diffText: newFilePatch, message: 'fix: r5' });
  ok('R5 分支不存在 → branch_not_found（默认不静默建分支）', c5.ok === false && c5.reason === 'branch_not_found', c5);
  // R5b: 显式 createBranch → 允许以本提交创建新分支
  const c5b = await pa.pushCommit({ repoUrl: bare, headSha: newTip, diffText: newFilePatch,
    branch: 'created/probe-branch', message: 'fix: r5b', credentialToken: null, createBranch: true });
  ok('R5b 显式 createBranch:true → 新分支创建成功',
    c5b.ok === true && c5b.created_branch === true, c5b);
  const createdTip = execFileSync('git', ['--git-dir', bare, 'rev-parse', 'refs/heads/created/probe-branch']).toString().trim();
  ok('R5c 新分支 tip=本提交', createdTip === c5b.commitSha, { createdTip });

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

// ── PG 组：服务层门 + 原子认领 + 两阶段写 + 读回对账 + 合并资格门 ──
// 隔离纪律：一次性 PG 容器 + 本地裸仓做"远端"（fetch mock 拦平台 API）；
// prepare/push/repoUrl 经 deps 注入指向本地仓——服务代码路径与生产同一条。
const RUN = crypto.randomUUID();
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

  // 本地"远端"裸仓（真实 push 语义）
  const srcDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-pg-src-'));
  const bareDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mp-pg-bare-')) + '/remote.git';
  const gp = (cwd, args) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t', ...args],
    { cwd, stdio: 'pipe' }).toString();
  gp(bareDir.replace('/remote.git', ''), ['init', '-q', '--bare', 'remote.git']);
  gp(srcDir, ['init', '-q', '-b', 'fix-branch']);
  execFileSync('git', ['config', 'core.autocrlf', 'false'], { cwd: srcDir, stdio: 'pipe' });
  fs.mkdirSync(path.join(srcDir, 'src'), { recursive: true });
  fs.writeFileSync(path.join(srcDir, 'src', 'config.js'), 'const x = 1;\nconst token = "ghp_x";\nconst y = 2;\n');
  gp(srcDir, ['add', '.']); gp(srcDir, ['commit', '-qm', 'v1']);
  gp(srcDir, ['remote', 'add', 'origin', bareDir]);
  gp(srcDir, ['push', '-q', 'origin', 'fix-branch']);
  const BRANCH_HEAD = gp(srcDir, ['rev-parse', 'HEAD']).trim();
  const headForBranch = () => execFileSync('git', ['--git-dir', bareDir, 'rev-parse',
    'refs/heads/fix-branch']).toString().trim();

  await pool.query(
    `INSERT INTO mu.repository (repo_id, tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,$2,'gitee','50740631','nghqqa','mergepilot-test')`, [REPO, T]);
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,2,$4,'open')`, [PRID, T, REPO, HEAD]);
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUN, T, REPO, PRID, BRANCH_HEAD]);
  // GOOD_DIFF 与本地仓 src/config.js 初版内容严格对应（可真实 git apply）：
  // 文件三行 = const x = 1; / const token = "ghp_x"; / const y = 2;
  const GOOD_DIFF_APPLY = [
    '--- a/src/config.js', '+++ b/src/config.js', '@@ -1,3 +1,4 @@',
    ' const x = 1;', '-const token = "ghp_x";', '+const token = process.env.TOKEN;',
    ' const y = 2;', '+const audit = true;',
  ].join('\n') + '\n';
  const pSeed = await art.persistFixPatch(pool, { runId: RUN, tenantId: T, repoId: REPO,
    headSha: BRANCH_HEAD, diffText: GOOD_DIFF_APPLY, rationale: 'move token to env',
    changedFiles: [{ path: 'src/config.js' }] });
  ok('P0 种子补丁入库', pSeed.ok === true && pSeed.patchId, pSeed);
  const DIG = (await pool.query(`SELECT patch_digest FROM mu.fix_patch WHERE run_id=$1`, [RUN])).rows[0].patch_digest;

  const env = { ...process.env, MU_GITEE_PAT: 'testpat' };

  // fetch mock：PR 读取 + CI 状态 + 合并 API（gitee 平台面；凭据走 env）
  const remoteState = { prHead: BRANCH_HEAD, prState: 'open', mergeable: true, mergeSha: null,
    ciState: 'success', ciStatuses: [{ state: 'success', context: 'ci/test' }], mergeLost: false };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url).split('?')[0]; const m = opts.method || 'GET';
    if (/\/pulls\/2$/.test(u) && m === 'GET') {
      return jsonResp({ state: remoteState.prState, head: { sha: remoteState.prHead, ref: 'fix-branch' },
        base: { ref: 'master' }, mergeable: remoteState.mergeable,
        merge_commit_sha: remoteState.mergeSha });
    }
    if (/\/commits\/[0-9a-f]+\/status$/.test(u)) {
      return jsonResp({ state: remoteState.ciState, statuses: remoteState.ciStatuses });
    }
    if (/\/pulls\/2\/merge$/.test(u) && m === 'PUT') {
      if (remoteState.mergeLost) throw new Error('timeout: merge response lost');
      remoteState.prState = 'merged'; remoteState.mergeSha = 'c'.repeat(40);
      return jsonResp({ sha: remoteState.mergeSha });
    }
    return jsonResp({}, 404);
  };

  // deps 注入：prepare/push/repoUrl 指向本地裸仓（与服务生产路径同一代码）
  const pa = await import('../lib/multiuser/patch-applicability.mjs');
  const mkDeps = (over = {}) => ({
    repoUrlFor: () => bareDir,
    prepareCommit: over.prepareCommit ?? ((a) => pa.prepareCommit({ ...a, repoUrl: bareDir })),
    pushPrepared: over.pushPrepared ?? ((a) => pa.pushPrepared({ ...a, repoUrl: bareDir })),
    enqueueEventSync: over.enqueueEventSync ?? (async () => ({ job_id: crypto.randomUUID() })),
    merge: over.merge,
  });
  const binds = { expectedRemoteHead: BRANCH_HEAD, expectedPatchDigest: DIG };

  // ── 确认字段与绑定门 ──
  const g1 = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER, confirm: true });
  ok('F1 缺确认字段 → invalid_confirm_fields（零操作记录）',
    g1.ok === false && g1.reason === 'invalid_confirm_fields'
    && (await pool.query(`SELECT count(*)::int c FROM mu.fix_apply_operation`)).rows[0].c === 0, g1);
  const g2 = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER, confirm: true,
    expectedRemoteHead: BRANCH_HEAD, expectedPatchDigest: '0'.repeat(64) });
  ok('F2 同 head 补丁换版（digest 不符）→ patch_changed', g2.ok === false && g2.reason === 'patch_changed', g2);
  const g3 = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: crypto.randomUUID(),
    userId: USER, confirm: true, ...binds });
  ok('F3 跨租户 → run_not_found（同形拒绝）', g3.ok === false && g3.reason === 'run_not_found', g3);

  const v1 = await fa.previewApplyFix(pool, { env, runId: RUN, tenantId: T });
  ok('F4 预览：can_apply + confirm_binds（head+digest 绑定）+ 机器人身份',
    v1.ok === true && v1.can_apply === true
    && v1.confirm_binds?.expected_remote_head === BRANCH_HEAD
    && v1.confirm_binds?.expected_patch_digest === DIG
    && v1.robot_identity === 'MergePilot Gitee Bot', v1.confirm_binds);

  // ── 并发单赢家（可控同步点：胜方卡在 push 前，败方完成完整认领尝试）──
  let releasePush;
  const pushGate = new Promise((r) => { releasePush = r; });
  let winnerPushStarted = false;
  const slowPush = async (a) => { winnerPushStarted = true; await pushGate;
    return pa.pushPrepared({ ...a, repoUrl: bareDir }); };
  const p1 = fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER, confirm: true,
    ...binds, deps: mkDeps({ pushPrepared: slowPush }) });
  await sleep(1500);
  ok('F5 并发窗口：胜方 executing 中（可控同步点验证）', winnerPushStarted === true
    && (await pool.query(`SELECT status FROM mu.fix_apply_operation WHERE run_id=$1`, [RUN])).rows[0].status === 'executing',
    null);
  const loser = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER,
    confirm: true, ...binds, deps: mkDeps() });
  ok('F6 并发败方 → operation_in_progress 且不覆写胜方记录',
    loser.ok === false && loser.reason === 'operation_in_progress'
    && (await pool.query(`SELECT status FROM mu.fix_apply_operation WHERE run_id=$1`, [RUN])).rows[0].status === 'executing',
    loser);
  releasePush();
  const w1 = await p1;
  ok('F7 胜方提交成功 → committed + 远端 tip=新提交 + 重审入队',
    w1.ok === true && w1.status === 'committed' && headForBranch() === w1.commit_sha
    && w1.review_reenqueue?.ok === true, { ok: w1.ok, reason: w1.reason, detail: w1.detail, sha: w1.commit_sha, enq: w1.review_reenqueue });
  const WIN_SHA = w1.commit_sha;

  // 幂等：重复确认 → 返回既有结果，零新 push
  const tipBefore = headForBranch();
  const i1 = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER,
    confirm: true, ...binds, deps: mkDeps() });
  ok('F8 已应用重复确认 → 幂等返回且零新写入',
    i1.ok === true && i1.idempotent === true && i1.commit_sha === WIN_SHA
    && headForBranch() === tipBefore, i1);

  // F8b: push 落地后远端 head 已推进（模拟 PR 对象同步）——重复确认仍须幂等返回
  remoteState.prHead = WIN_SHA;
  const i1b = await fa.confirmApplyFix(pool, { env, runId: RUN, tenantId: T, userId: USER,
    confirm: true, ...binds, deps: mkDeps() });
  ok('F8b head 已推进后的重复确认 → 仍幂等（幂等先于远端检查）',
    i1b.ok === true && i1b.idempotent === true && i1b.commit_sha === WIN_SHA, i1b);

  const v2 = await fa.previewApplyFix(pool, { env, runId: RUN, tenantId: T });
  ok('F9 已应用后预览 → blocked already_applied', v2.can_apply === false
    && v2.blocked_reason === 'already_applied', v2.blocked_reason);

  remoteState.prHead = WIN_SHA; // push 生效：mock 远端 head 推进
  // ── 重审入队失败可恢复（push 已落地，不回退不重推）──
  const RUNB = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUNB, T, REPO, PRID, WIN_SHA]);
  const diff2 = ['--- a/src/config.js', '+++ b/src/config.js', '@@ -1,4 +1,5 @@',
    ' const x = 1;', ' const token = process.env.TOKEN;', ' const y = 2;',
    ' const audit = true;', '+const flagged = 1;'].join('\n') + '\n';
  const pSeed2 = await art.persistFixPatch(pool, { runId: RUNB, tenantId: T, repoId: REPO,
    headSha: WIN_SHA, diffText: diff2, rationale: 'r2', changedFiles: [{ path: 'src/config.js' }] });
  // 远端分支仍在 WIN_SHA（F7 推进后未动）——直接可应用
  const DIGB = (await pool.query(`SELECT patch_digest FROM mu.fix_patch WHERE run_id=$1 AND active`, [RUNB])).rows[0].patch_digest;
  const b1 = await fa.confirmApplyFix(pool, { env, runId: RUNB, tenantId: T, userId: USER,
    confirm: true, expectedRemoteHead: WIN_SHA, expectedPatchDigest: DIGB,
    deps: mkDeps({ enqueueEventSync: async () => { throw new Error('queue down'); } }) });
  ok('F10 入队失败：apply 仍成功 + review_reenqueue.ok=false（可恢复）',
    b1.ok === true && b1.status === 'committed' && b1.review_reenqueue?.ok === false, { b1 });
  const B1_SHA = b1.commit_sha;

  // webhook/重复入队去重：同 head 已有 run → deduped
  const e1 = await fa.enqueueReReview(pool, { env, run: { tenant_id: T, repo_id: REPO, pr_id: PRID,
    run_id: RUNB, provider_pr_number: 2 }, headSha: WIN_SHA, userId: USER,
    deps: { enqueueEventSync: async () => ({ job_id: 'x' }) } });
  ok('F11 重审入队幂等：同 head 已有 run → deduped=true', e1.ok === true && e1.deduped === true, e1);

  // ── 推送响应丢失 → result_uncertain → 读回对账定案 ──
  const RUNC = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUNC, T, REPO, PRID, B1_SHA]);
  const diffC = ['--- a/src/config.js', '+++ b/src/config.js', '@@ -1,5 +1,6 @@',
    ' const x = 1;', ' const token = process.env.TOKEN;', ' const y = 2;',
    ' const audit = true;', ' const flagged = 1;', '+const gated = 1;'].join('\n') + '\n';
  const pC = await art.persistFixPatch(pool, { runId: RUNC, tenantId: T, repoId: REPO,
    headSha: B1_SHA, diffText: diffC, rationale: 'rc', changedFiles: [{ path: 'src/config.js' }] });
  const DIGC = (await pool.query(`SELECT patch_digest FROM mu.fix_patch WHERE run_id=$1 AND active`, [RUNC])).rows[0].patch_digest;
  remoteState.prHead = B1_SHA;
  const tipC = headForBranch(); // = B1_SHA（分支当前 tip）
  // 真实 push 后抛 timeout（响应丢失）——远端实际已写入
  const lostPush = async (a) => {
    const r = await pa.pushPrepared({ ...a, repoUrl: bareDir });
    if (!r.ok) return r;
    throw new Error('timeout: push response lost');
  };
  const u1 = await fa.confirmApplyFix(pool, { env, runId: RUNC, tenantId: T, userId: USER,
    confirm: true, expectedRemoteHead: tipC, expectedPatchDigest: DIGC,
    deps: mkDeps({ pushPrepared: lostPush }) });
  const opCRow = (await pool.query(
    `SELECT status, candidate_commit_sha FROM mu.fix_apply_operation
      WHERE run_id=$1 AND operation_type='apply_fix'`, [RUNC])).rows[0];
  ok('F12 push 成功但响应丢失 → result_uncertain（不判失败不自动重试；候选 SHA 已持久化）',
    u1.ok === false && u1.reason === 'result_uncertain' && u1.uncertain === true
    && opCRow?.status === 'result_uncertain' && Boolean(opCRow?.candidate_commit_sha),
    { reason: u1.reason, op: opCRow });
  remoteState.prHead = headForBranch(); // 丢失响应的 push 实际已落地——mock 远端与分支对齐
  const opC = (await pool.query(
    `SELECT operation_id FROM mu.fix_apply_operation WHERE run_id=$1 AND operation_type='apply_fix'`, [RUNC])).rows[0];
  const rc1 = await fa.reconcileOperation(pool, { env, operationId: opC.operation_id, tenantId: T,
    deps: { repoUrlFor: () => bareDir } });
  ok('F13 对账：候选在远端 → committed（reconciled，提交成功仅响应丢失）',
    rc1.ok === true && rc1.reconciled === true && rc1.status === 'committed', rc1);
  const rc1b = await fa.reconcileOperation(pool, { env, operationId: opC.operation_id, tenantId: T,
    deps: { repoUrlFor: () => bareDir } });
  ok('F13b 对账幂等：已终态 no-op', rc1b.ok === true && rc1b.reconciled === false
    && rc1b.status === 'committed', rc1b);

  // F13c: 对账竞态门——仍在执行窗口内（10 分钟活跃）的 executing 拒绝对账
  {
    const tipLive = headForBranch();
    const insLive = await pool.query(
      `INSERT INTO mu.fix_apply_operation
         (operation_type, status, tenant_id, repo_id, pr_id, run_id, patch_id, patch_digest,
          provider, provider_pr_number, source_branch, target_branch, expected_head,
          robot_identity, idempotency_key, operator_user_id)
       VALUES ('apply_fix','executing',$1,$2,$3,$4,$5,$6,'gitee',2,'fix-branch','master',$7,
         'MergePilot Gitee Bot',$8,$9) RETURNING operation_id`,
      [T, REPO, PRID, RUNC, pC.patchId ?? null, DIGC, tipLive,
       `${RUNC}:apply_fix:live`, USER]);
    const rcLive = await fa.reconcileOperation(pool, { env, operationId: insLive.rows[0].operation_id,
      tenantId: T, deps: { repoUrlFor: () => bareDir } });
    ok('F13c 对账竞态门：executing 且 10 分钟内活跃 → 拒绝对账（不释放活执行者）',
      rcLive.ok === true && rcLive.reconciled === false
      && /执行窗口/.test(rcLive.note ?? ''), rcLive);
  }

  // ── U 组：uncertain 禁止直接重认领 → 对账定案 committed → 重审关联补齐（R1/R2/R4）──
  {
    const tipU = headForBranch();
    const RUNU = crypto.randomUUID();
    await pool.query(
      `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
       VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUNU, T, REPO, PRID, tipU]);
    const diffU = ['--- a/src/config.js', '+++ b/src/config.js', '@@ -1,6 +1,7 @@',
      ' const x = 1;', ' const token = process.env.TOKEN;', ' const y = 2;',
      ' const audit = true;', ' const flagged = 1;', ' const gated = 1;', '+const probed = 1;'].join('\n') + '\n';
    await art.persistFixPatch(pool, { runId: RUNU, tenantId: T, repoId: REPO,
      headSha: tipU, diffText: diffU, rationale: 'ru', changedFiles: [{ path: 'src/config.js' }] });
    const DIGU = (await pool.query(`SELECT patch_digest FROM mu.fix_patch WHERE run_id=$1 AND active`, [RUNU])).rows[0].patch_digest;
    // 第一次确认：真 push 后响应丢失 → uncertain（R12 同型，但独立 run）
    const u1 = await fa.confirmApplyFix(pool, { env, runId: RUNU, tenantId: T, userId: USER,
      confirm: true, expectedRemoteHead: tipU, expectedPatchDigest: DIGU,
      deps: mkDeps({ pushPrepared: lostPush }) });
    ok('U1 响应丢失 → result_uncertain', u1.ok === false && u1.reason === 'result_uncertain', u1.reason);
    // 第二次确认（直接重认领）→ 必须被拒并指向对账
    const u2 = await fa.confirmApplyFix(pool, { env, runId: RUNU, tenantId: T, userId: USER,
      confirm: true, expectedRemoteHead: tipU, expectedPatchDigest: DIGU });
    ok('U2 uncertain 直接重确认 → result_uncertain_requires_reconcile（R1）',
      u2.ok === false && u2.reason === 'result_uncertain_requires_reconcile', u2);
    // 对账（候选已成 tip 或祖先——R2 全量取历史判定）+ 重审关联补齐（R4）
    const opU = (await pool.query(
      `SELECT operation_id FROM mu.fix_apply_operation WHERE run_id=$1 AND operation_type='apply_fix'`, [RUNU])).rows[0];
    const u3 = await fa.reconcileOperation(pool, { env, operationId: opU.operation_id, tenantId: T,
      deps: { repoUrlFor: () => bareDir, enqueueEventSync: async (p) => ({ job_id: crypto.randomUUID(), head: p.headSha }) } });
    ok('U3 对账定案 committed + 重审关联补齐（R2 全量历史祖先判定 + R4）',
      u3.ok === true && u3.reconciled === true && u3.status === 'committed'
      && u3.review_reenqueue?.ok === true, u3);
    // 对账后重复确认 → 幂等（不重复应用补丁）
    const u4 = await fa.confirmApplyFix(pool, { env, runId: RUNU, tenantId: T, userId: USER,
      confirm: true, expectedRemoteHead: tipU, expectedPatchDigest: DIGU });
    ok('U4 对账收口后重复确认 → 幂等返回（R4 禁止重复应用）',
      u4.ok === true && u4.idempotent === true && u4.commit_sha === u1.candidate_commit_sha, u4);
  }

  // ── 写前进程退出（无候选）→ 对账 failed no_write_happened（可安全重试）──
  const tipNow = headForBranch();
  const opD = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.fix_apply_operation
       (operation_type, status, tenant_id, repo_id, pr_id, run_id, patch_id, patch_digest,
        provider, provider_pr_number, source_branch, target_branch, expected_head,
        robot_identity, idempotency_key, operator_user_id, updated_at)
     VALUES ('apply_fix','executing',$1,$2,$3,$4,$5,$6,'gitee',2,'fix-branch','master',$7,
       'MergePilot Gitee Bot',$8,$9, now() - interval '11 minutes') RETURNING operation_id`,
    [T, REPO, PRID, RUNC, pC.patchId ?? null, DIGC, tipNow,
     `${RUNC}:apply_fix:deadbeef`, USER]);
  const opDReal = (await pool.query(
    `SELECT operation_id FROM mu.fix_apply_operation WHERE idempotency_key=$1`,
    [`${RUNC}:apply_fix:deadbeef`])).rows[0].operation_id;
  const rc2 = await fa.reconcileOperation(pool, { env, operationId: opDReal, tenantId: T,
    deps: { repoUrlFor: () => bareDir } });
  ok('F14 写前退出恢复：无候选+分支未推进 → failed no_write_happened（可重试）',
    rc2.ok === true && rc2.reconciled === true && rc2.status === 'failed'
    && (await pool.query(`SELECT error_code FROM mu.fix_apply_operation WHERE operation_id=$1`, [opDReal])).rows[0].error_code === 'no_write_happened',
    rc2);

  // ── 合并资格门 ──
  remoteState.prHead = headForBranch(); // U 组推进后 mock 远端与本地分支对齐
  const HEAD_M = headForBranch();
  const RUNM = crypto.randomUUID();
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUNM, T, REPO, PRID, HEAD_M]);
  remoteState.ciStatuses = []; remoteState.ciState = null;
  const mg0 = await fa.previewMergePr(pool, { env, runId: RUNM, tenantId: T });
  ok('F15 资格门：CI 未配置 → 阻塞（ci_not_configured，如实不冒充）',
    mg0.ok === true && mg0.can_merge === false
    && mg0.blocked.some((b) => b.code === 'ci_not_configured'), mg0.blocked);
  remoteState.ciStatuses = [{ state: 'pending' }]; remoteState.ciState = 'pending';
  const mg1 = await fa.previewMergePr(pool, { env, runId: RUNM, tenantId: T });
  ok('F16 资格门：CI pending → 阻塞', mg1.can_merge === false
    && mg1.blocked.some((b) => b.code === 'ci_pending'), mg1.blocked);
  remoteState.ciStatuses = [{ state: 'failure' }]; remoteState.ciState = 'failure';
  const mg2 = await fa.previewMergePr(pool, { env, runId: RUNM, tenantId: T });
  ok('F17 资格门：CI failed → 阻塞', mg2.can_merge === false
    && mg2.blocked.some((b) => b.code === 'ci_failed'), mg2.blocked);
  remoteState.ciStatuses = [{ state: 'success' }]; remoteState.ciState = 'success';
  // P0/P1 未处理 → 阻塞
  const att = await (await import('../lib/multiuser/orchestration.mjs')).claimNextAttempt(pool,
    { runId: RUNM, agentRole: 'reviewer', provider: 'deterministic', maxAttempts: 3,
      tenantId: T, repoId: REPO, prId: PRID, headSha: HEAD_M });
  await pool.query(
    `INSERT INTO mu.agent_finding (finding_id, attempt_id, run_id, tenant_id, repo_id, pr_id, head_sha,
       rule_id, severity, confidence, path, line_start, line_end, title, summary_masked)
     VALUES (gen_random_uuid(),$6,$1,$2,$3,$4,$5,'R-X','P1',0.9,'a.js',1,1,'t','m')`,
    [RUNM, T, REPO, PRID, HEAD_M, att.attemptId]);
  const mg3 = await fa.previewMergePr(pool, { env, runId: RUNM, tenantId: T });
  ok('F18 资格门：当前 head 有未处理 P1 → 阻塞（旧 head 审查不可替代）',
    mg3.can_merge === false && mg3.blocked.some((b) => b.code === 'blocking_findings_open'), mg3.blocked);
  await pool.query(`DELETE FROM mu.agent_finding WHERE run_id=$1`, [RUNM]);
  const mg4 = await fa.previewMergePr(pool, { env, runId: RUNM, tenantId: T });
  ok('F19 资格门全过 → can_merge + confirm_binds.head',
    mg4.ok === true && mg4.can_merge === true && mg4.confirm_binds?.expected_head === HEAD_M, mg4.blocked);
  // mergeable 未知 → 阻塞
  remoteState.mergeable = null;
  const mg5 = await fa.previewMergePr(pool, { env, runId: RUNM, tenantId: T });
  ok('F20 资格门：mergeable 未知 → 阻塞（不放行）',
    mg5.can_merge === false && mg5.blocked.some((b) => b.code === 'mergeable_unknown'), mg5.blocked);
  remoteState.mergeable = true;

  // 合并响应丢失 → uncertain → 对账 merged
  remoteState.mergeLost = true;
  const mg6 = await fa.confirmMergePr(pool, { env, runId: RUNM, tenantId: T, userId: USER,
    confirm: true, expectedHead: HEAD_M, deps: mkDeps() });
  ok('F21 合并响应丢失 → result_uncertain', mg6.ok === false && mg6.reason === 'result_uncertain', mg6);
  remoteState.mergeLost = false;
  remoteState.prState = 'merged'; remoteState.mergeSha = 'd'.repeat(40); // 平台实际已合并
  const opM = (await pool.query(
    `SELECT operation_id FROM mu.fix_apply_operation WHERE run_id=$1 AND operation_type='merge_pr'`, [RUNM])).rows[0];
  const rc3 = await fa.reconcileOperation(pool, { env, operationId: opM.operation_id, tenantId: T });
  ok('F22 对账合并：PR state=merged → merged（读回定案）',
    rc3.ok === true && rc3.reconciled === true && rc3.status === 'merged', rc3);

  // 已合并后重复确认 → 幂等返回（不被 pr_open 提前拒绝）
  const mg7 = await fa.confirmMergePr(pool, { env, runId: RUNM, tenantId: T, userId: USER,
    confirm: true, expectedHead: remoteState.prHead });
  ok('F23 已合并重复确认 → 幂等返回（修复 pr_open 提前拒绝缺陷）',
    mg7.ok === true && mg7.idempotent === true && mg7.merge_sha === 'd'.repeat(40), mg7);

  const mg8 = await fa.confirmMergePr(pool, { env, runId: RUNM, tenantId: T, userId: USER, confirm: true });
  ok('F24 合并缺 expected_head → invalid_confirm_fields', mg8.ok === false
    && mg8.reason === 'invalid_confirm_fields', mg8);

  // 操作记录全景
  const opsAll = (await pool.query(
    `SELECT operation_type, status, error_code FROM mu.fix_apply_operation ORDER BY created_at`)).rows;
  ok('F25 操作记录全景：committed（含对账收口）/ failed(no_write) / merged 齐备（uncertain 为过渡态不残留）',
    opsAll.filter((o) => o.status === 'committed' && o.operation_type === 'apply_fix').length >= 2
    && opsAll.some((o) => o.status === 'failed' && o.error_code === 'no_write_happened')
    && opsAll.some((o) => o.status === 'merged' && o.operation_type === 'merge_pr')
    && !opsAll.some((o) => o.status === 'result_uncertain'), opsAll);

  globalThis.fetch = realFetch;
  await pool.end();
  for (const d of [srcDir, bareDir.replace('/remote.git', '')]) {
    try { fs.rmSync(d, { recursive: true, force: true }); } catch { /* 尽力 */ }
  }
  console.log(`\nfix-apply.integration: ${pass} pass / ${fail} fail`);
  process.exit(fail ? 1 : 0);
} catch (e) {
  console.error('HARNESS ERROR', e);
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* 尽力 */ }
  process.exit(1);
} finally {
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* 尽力 */ }
}
