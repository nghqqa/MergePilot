// mu-artifacts.integration.mjs — 审查产物交付波集成测试（一次性 PG + 全局 fetch mock）。
// 覆盖：diff 解析/可应用性/补丁持久化、链输出幂等、报告构造（marker/边界声明）、
//       发布流程（create-on-missing / update-on-existing-marker / stale head 拒绝 / 失败状态）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import path from 'node:path';
import { createRequire } from 'node:module';

const HERE = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Z]:)/, '$1'));
const { Pool } = createRequire(path.join(HERE, 'support/noop.js'))('pg');

let pass = 0, fail = 0;
const ok = (name, cond, detail) => {
  if (cond) { pass += 1; console.log('  PASS ', name); }
  else { fail += 1; console.log('  FAIL ', name, JSON.stringify(detail ?? '').slice(0, 240)); }
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const art = await import('../lib/multiuser/artifacts.mjs');
const pub = await import('../lib/multiuser/publish.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');

const RUN = crypto.randomUUID();
const TEN = crypto.randomUUID();
const REPO = crypto.randomUUID();
const PRID = crypto.randomUUID();
const HEAD = 'abbfe8a157172ef4f96a2437bf2114374b9fa378';

// ── 纯函数单测 ──
const GOOD_DIFF = [
  '--- a/src/config.js', '+++ b/src/config.js', '@@ -1,3 +1,4 @@',
  ' const x = 1;', '-const token = "ghp_x";', '+const token = process.env.TOKEN;', '+const y = 2;',
].join('\n');

const p1 = art.parseUnifiedDiff(GOOD_DIFF);
ok('U1 合法统一 diff 解析（文件+增删行数）', p1.ok === true && p1.files?.[0]?.path === 'src/config.js'
  && p1.files[0].deletions === 1 && p1.files[0].additions === 2, p1);
const p2 = art.parseUnifiedDiff('not a diff at all');
ok('U2 非 diff 输入 → not_unified_diff', p2.ok === false && p2.reason === 'not_unified_diff', p2);
const p3 = art.parseUnifiedDiff('--- a/etc/passwd\n+++ /etc/shadow\n@@ -1 +1 @@\n-x\n+y\n');
ok('U3 越界/绝对路径 → unsafe_path', p3.ok === false && p3.reason === 'unsafe_path', p3);

const a1 = art.checkPatchApplicability(GOOD_DIFF, [{ path: 'src/config.js' }]);
ok('A1 可应用：触及文件在变更集内', a1.ok === true && a1.checks.includes('path_scope'), a1);
const a2 = art.checkPatchApplicability(GOOD_DIFF, [{ path: 'other/file.js' }]);
ok('A2 触及集外文件 → 不可应用+issues', a2.ok === false && a2.issues[0].startsWith('path_not_in_change_set'), a2);

const runFixture = { run_id: RUN, status: 'COMPLETED', llm_mode: 'deterministic_only', head_sha: HEAD };
const findings = [{ rule_id: 'R-SECRET', severity: 'P0', path: 'src/config.js', line_start: 2,
  title: 'x', summary_masked: 'const token = "ghp_***";', remediation: 'move to env' }];
const body = art.buildPublishComment({ run: runFixture, findings, patch: null,
  verification: ['静态规则检查：已执行', '实际测试执行：未执行'],
  repoLabel: 'nghqqa/mergepilot-test', prNumber: 2, headSha: HEAD, detailUrl: null, reviewedAt: null });
ok('C1 发布评论含 marker + 边界声明 + 未修改代码声明',
  art.publishMarker(RUN, HEAD) !== '' && body.includes('未修改代码') && body.includes('实际测试执行：未执行'), body.slice(0, 80));
ok('C2 发布评论不含敏感原值（ghp_ 原文不出现）', !body.includes('ghp_x'), null);

const report = art.buildMarkdownReport({ run: runFixture, findings, attempts: [], outputs: {},
  patch: null, verification: ['实际测试执行：未执行'], repoLabel: 'nghqqa/mergepilot-test',
  prNumber: 2, headSha: HEAD });
ok('C3 报告含 head/发现/未验证声明', report.includes(HEAD.slice(0, 8)) && report.includes('未验证'), null);

// ── 权威口径（真实可应用性以 git_apply 为准——结构通过不得宣称可应用）──
const failPatch = { files: [{ path: 'src/config.js' }], head_sha: HEAD, patch_version: 1,
  patch_digest: 'a'.repeat(64),
  applicability: { ok: true, issues: [], checks: ['format'],
    git_apply: { verified: true, ok: false, method: 'git apply --check',
      output: 'git apply --check 失败: error: corrupt patch at line 8' } } };
const bodyFail = art.buildPublishComment({ run: runFixture, findings, patch: failPatch,
  verification: [], repoLabel: 'o/r', prNumber: 2, headSha: HEAD, detailUrl: null, reviewedAt: null });
ok('C4 结构通过+真实失败 → 评论明确"补丁不可应用"', bodyFail.includes('不可应用'), bodyFail.slice(0, 400));
ok('C5 评论不再以结构 ok 宣称可应用', !/可应用性\s+"?true"?/.test(bodyFail), null);
const reportFail = art.buildMarkdownReport({ run: runFixture, findings, attempts: [], outputs: {},
  patch: failPatch, verification: [], repoLabel: 'o/r', prNumber: 2, headSha: HEAD });
ok('C6 报告验证三分含真实失败+digest', reportFail.includes('失败——补丁不可应用')
  && reportFail.includes('格式与路径检查') && reportFail.includes('实际修复测试'), null);
const tvPass = { verified: true, ok: true, test_cmd: 'node tests/bug.test.js',
  pre: { code: 1 }, post: { code: 0 } };
const lv = art.patchVerificationLines({ files: [], applicability: { ok: true, issues: [] },
  patch_digest: 'a'.repeat(64), test_verification: tvPass });
ok('C7 实际测试通过行=修复前败→修复后过', lv.some((l) => l.includes('实际修复测试：通过') && l.includes('修复前失败→修复后通过')), lv);
const tvPre = { verified: true, ok: false, reason: 'pre_test_passed', pre: { code: 0 }, post: null };
ok('C8 修复前即通过=不能证明修复（如实）',
  art.patchVerificationLines({ applicability: null, test_verification: tvPre })
    .some((l) => l.includes('未通过')), null);
ok('C9 未执行测试如实标注',
  art.patchVerificationLines({ applicability: null }).some((l) => l.includes('未执行')), null);

// ── 管线级（一次性 PG）──
const CTR = `mu-art-${crypto.randomBytes(3).toString('hex')}`;
const PORT = 17500 + Math.floor(Math.random() * 60);
try {
  execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
  const { createMuStore } = await import('../lib/multiuser/store.mjs');
  const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
  for (let i = 0; i < 150; i++) { try { await pool.query('SELECT 1'); break; } catch { await sleep(400); } }
  const store = await createMuStore({ pool }); await store.initSchema(); await store.bootstrap();
  const T = (await pool.query(`SELECT tenant_id FROM mu.tenant LIMIT 1`)).rows[0].tenant_id;

  // ── 持久化：链输出幂等 + 补丁存储 ──
  await art.persistChainOutputs(pool, { runId: RUN, tenantId: T, results: { verifier: { verdict: 'PASS' } } });
  await art.persistChainOutputs(pool, { runId: RUN, tenantId: T, results: { verifier: { verdict: 'PASS', note: 'n' } } });
  const rowsOut = (await pool.query(`SELECT content FROM mu.review_output WHERE run_id=$1 AND agent_role='verifier'`, [RUN])).rows;
  ok('D1 链输出幂等（UNIQUE upsert 单行）', rowsOut.length === 1 && rowsOut[0].content.note === 'n', rowsOut);

  const okPatch = await art.persistFixPatch(pool, { runId: RUN, tenantId: T, repoId: REPO,
    headSha: HEAD, diffText: GOOD_DIFF, rationale: 'move token to env', changedFiles: [{ path: 'src/config.js' }] });
  const badPatch = await art.persistFixPatch(pool, { runId: RUN, tenantId: T, repoId: REPO,
    headSha: HEAD, diffText: 'garbage', rationale: null, changedFiles: [] });
  const patchRow = (await pool.query(`SELECT diff_text, applicability FROM mu.fix_patch WHERE run_id=$1`, [RUN])).rows[0];
  ok('D2 合法补丁入库+可应用性 ok', okPatch.ok === true && okPatch.applicability?.ok === true, okPatch);
  ok('D3 非法补丁拒绝（not_unified_diff）', badPatch.ok === false && badPatch.reason === 'not_unified_diff', badPatch);
  ok('D4 入库内容与保存产物一致', patchRow?.diff_text === GOOD_DIFF, null);

  // 版本化闭环：修正=新版本行（原文+诊断保留）；active 唯一；digest 绑定；correction 链
  const v2diff = GOOD_DIFF + '\n';
  const v2 = await art.persistFixPatch(pool, { runId: RUN, tenantId: T, repoId: REPO,
    headSha: HEAD, diffText: v2diff, rationale: 'deterministic fix: add trailing newline',
    changedFiles: [{ path: 'src/config.js' }], correctionOf: okPatch.patchId,
    correctionKind: 'deterministic' });
  const vers = (await pool.query(
    `SELECT patch_id, patch_version, active, patch_digest, correction_of, correction_kind, diff_text
       FROM mu.fix_patch WHERE run_id=$1 ORDER BY patch_version`, [RUN])).rows;
  ok('D5 修正=新版本行（v2）且原始版本保留', v2.ok === true && vers.length === 2
    && vers[0].patch_version === 1 && vers[1].patch_version === 2
    && vers[0].diff_text === GOOD_DIFF, { len: vers.length, v2: v2.patchVersion });
  ok('D6 active 唯一（仅 v2）', vers[0].active === false && vers[1].active === true, vers.map((r) => r.active));
  ok('D7 digest 绑定各版本', vers[0].patch_digest === art.sha256Hex(GOOD_DIFF)
    && vers[1].patch_digest === art.sha256Hex(v2diff), null);
  ok('D8 correction 链（correction_of/kind）', vers[1].correction_of === okPatch.patchId
    && vers[1].correction_kind === 'deterministic', vers[1]);

  // ── 发布流程（seed 仓库/PR/run + 全局 fetch mock）──
  await pool.query(
    `INSERT INTO mu.repository (repo_id, tenant_id, provider, provider_repo_id, owner, name)
     VALUES ($1,$2,'gitee','50740631','nghqqa','mergepilot-test')`, [REPO, T]);
  await pool.query(
    `INSERT INTO mu.pull_request (pr_id, tenant_id, repo_id, provider_pr_number, head_sha, state)
     VALUES ($1,$2,$3,2,$4,'open')`, [PRID, T, REPO, HEAD]);
  await pool.query(
    `INSERT INTO mu.review_run (run_id, tenant_id, repo_id, pr_id, head_sha, trigger_source, status, llm_mode)
     VALUES ($1,$2,$3,$4,$5,'manual','COMPLETED','deterministic_only')`, [RUN, T, REPO, PRID, HEAD]);
  // agent_finding.attempt_id NOT NULL：绑定 deterministic reviewer attempt 行
  const att = await orch.claimNextAttempt(pool, { runId: RUN, agentRole: 'reviewer',
    provider: 'deterministic', maxAttempts: 3, tenantId: T, repoId: REPO, prId: PRID, headSha: HEAD });
  await pool.query(
    `INSERT INTO mu.agent_finding (finding_id, attempt_id, run_id, tenant_id, repo_id, pr_id, head_sha,
       rule_id, severity, confidence, path, line_start, line_end, title, summary_masked)
     VALUES (gen_random_uuid(),$6,$1,$2,$3,$4,$5,'R-SECRET','P0',0.9,'src/config.js',2,2,'x','ghp_***')`,
    [RUN, T, REPO, PRID, HEAD, att.attemptId]);
  const remoteBody = [];

  const realFetch = globalThis.fetch;
  // 端点口径=#425 实测：Gitee PR 评论在 pulls 空间（list/POST=pulls/{n}/comments，PATCH=pulls/comments/{id}）
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    const m = opts.method || 'GET';
    if (u.includes('/pulls/comments/') && m === 'PATCH') {
      const b = JSON.parse(opts.body);
      remoteBody[0] = { id: 777, body: b.body };
      return { ok: true, status: 200, json: async () => ({ id: 777, html_url: 'https://gitee.com/x' }) };
    }
    if (u.includes('/pulls/2/comments') && m === 'POST') {
      const b = JSON.parse(opts.body);
      remoteBody.push({ id: 777, body: b.body });
      return { ok: true, status: 201, json: async () => ({ id: 777, html_url: 'https://gitee.com/x' }) };
    }
    if (u.includes('/pulls/2/comments') && m === 'GET') {
      return { ok: true, status: 200, json: async () => remoteBody };
    }
    if (/\/pulls\/2(\?|$)/.test(u)) {
      return { ok: true, status: 200, json: async () => ({ head: { sha: HEAD } }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };

  const env = { MU_GITEE_PAT: 'test-pat-not-real', MU_GITEE_INSTANCE_WEB_BASE: 'https://gitee.com' };
  const rr1 = await pub.publishRun(pool, { env, runId: RUN, tenantId: T, userId: null });
  ok('E1 create-on-missing：首次发布 published + 远端评论创建', rr1.ok === true && rr1.status === 'published'
    && remoteBody.length === 1 && rr1.remoteCommentId === '777', rr1);
  const rec1 = (await pool.query(`SELECT status, provider, head_sha FROM mu.pr_publish WHERE run_id=$1`, [RUN])).rows[0];
  ok('E2 发布记录落库（published + head + provider）', rec1?.status === 'published' && rec1?.provider === 'gitee', rec1);

  const rr2 = await pub.publishRun(pool, { env, runId: RUN, tenantId: T, userId: null });
  ok('E3 重复发布 → 更新既有评论（PATCH 命中 marker，不新建）', rr2.ok === true && rr2.updated === true
    && remoteBody.length === 1, { rr2, remote: remoteBody.length });

  // stale head：远端推进 → 拒绝发布且不触达评论
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (/\/pulls\/2(\?|$)/.test(u)) return { ok: true, status: 200, json: async () => ({ head: { sha: 'ddd' } }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const calls2 = [];
  const realFetch2 = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (opts.method === 'POST' || opts.method === 'PATCH') calls2.push(u);
    if (/\/pulls\/2(\?|$)/.test(u)) return { ok: true, status: 200, json: async () => ({ head: { sha: 'moved-on' } }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const rr3 = await pub.publishRun(pool, { env, runId: RUN, tenantId: T, userId: null });
  ok('E4 head 已变化 → 拒绝发布（409 语义），零评论写入', rr3.ok === false && rr3.stage === 'stale_head'
    && calls2.length === 0, { rr3, writes: calls2.length });

  // 失败状态：创建 500 → failed（不静默成功）
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (/\/pulls\/2(\?|$)/.test(u)) return { ok: true, status: 200, json: async () => ({ head: { sha: HEAD } }) };
    if (u.includes('/pulls/comments/') && opts.method === 'PATCH') {
      return { ok: false, status: 500, json: async () => ({}) };
    }
    if (u.includes('/pulls/2/comments') && (!opts.method || opts.method === 'GET')) {
      return { ok: true, status: 200, json: async () => [] };
    }
    if (u.includes('/pulls/2/comments') && opts.method === 'POST') {
      return { ok: false, status: 500, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  const rr4 = await pub.publishRun(pool, { env, runId: RUN, tenantId: T, userId: null });
  const rec4 = (await pool.query(`SELECT status FROM mu.pr_publish WHERE run_id=$1 ORDER BY created_at DESC LIMIT 1`, [RUN])).rows[0];
  ok('E5 远端创建 500 → 状态 failed（不伪造成功）', rr4.ok === false && rr4.stage === 'publish_failed'
    && rec4?.status === 'failed', { rr4, rec4 });

  globalThis.fetch = realFetch;
  await pool.end().catch(() => {});
} catch (e) {
  fail += 1; console.error('HARNESS ERROR', e);
  try { await pool.end(); } catch { /* */ }
} finally {
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
  console.log(`\n${pass} pass, ${fail} fail`);
  process.exit(fail ? 1 : 0);
}
