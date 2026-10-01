#!/usr/bin/env node
// console/backend/test/mu-fxv-prd.integration.mjs — Wave 3 PR-D 集成测试。
// 一次性 PG + 本地临时 git 仓库（file:// 契约）+ 真 FXV 子进程（worker-fixer/
// worker-verifier）+ mock GitHub provider。不访问真实 GitHub、无真实凭据。
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(here, 'support/noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const orch = await import('../lib/multiuser/orchestration.mjs');
const fxo = await import('../lib/multiuser/agents/fix-orchestrator.mjs');
const adapter = await import('../lib/multiuser/agents/fxv-adapter.mjs');
const { __setGhProviderForTests, __resetGhProvider } = await import('../lib/multiuser/ghprovider.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? (pass++, console.log('  PASS  ' + n)) : (fail++, console.log('  FAIL  ' + n + (d ? ' ' + JSON.stringify(d).slice(0, 160) : ''))); };

// ── 本地 git 仓库夹具（真子进程 worker 的 repo_url）──
const REPO_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'prd-repo-'));
const git = (...a) => execFileSync('git', ['-c', 'user.email=t@t', '-c', 'user.name=t',
  '-c', 'core.autocrlf=false', ...a], { cwd: REPO_DIR, encoding: 'utf8' });
const SECRET_LINE = 'const PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";';
fs.writeFileSync(path.join(REPO_DIR, 'src.js'), 'const a = 1;\n' + SECRET_LINE + '\nconst b = 2;\n');
git('init', '-q', '-b', 'main'); git('add', '.'); git('commit', '-qm', 'init');
const BASE_SHA = git('rev-parse', 'HEAD').trim();
const DIFF = `diff --git a/src.js b/src.js
index 111..222 100644
--- a/src.js
+++ b/src.js
@@ -1,2 +1,3 @@
 const a = 1;
+${SECRET_LINE}
 const b = 2;`;

const CTR = `prd-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 16600 + Math.floor(Math.random() * 60);
execFileSync('docker', ['run', '-d', '--name', CTR, '-e', 'POSTGRES_PASSWORD=x', '-e', 'POSTGRES_DB=mu',
  '-p', `127.0.0.1:${PORT}:5432`, 'postgres:16-alpine'], { stdio: 'pipe' });
const pool = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
for (let i = 0; i < 120; i++) { try { await pool.query('SELECT 1'); break; } catch { await new Promise((r) => setTimeout(r, 400)); } }

try {
  const store = await createMuStore({ pool, env: { MU_BOOTSTRAP_ADMIN_LOGIN: 'prd-admin' } });
  await store.initSchema(); await store.bootstrap();
  const T = (await store.ensureTenant({ slug: 'prd-t', displayName: 'PRD' })).tenant_id;
  const repo = await store.ensureRepository({ tenantId: T, provider: 'github',
    providerRepoId: '97001', owner: 'prd', name: 'repo', defaultBranch: 'main' });
  const INS = 78001, GHR = 97001, PRN = 5;
  await pool.query(`INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
    VALUES ($1,$2,1,'prd-owner',1) ON CONFLICT DO NOTHING`, [INS, T]);
  await pool.query(`INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state)
    VALUES ($1,$2,$3,'prd','repo',$4,'main','active') ON CONFLICT DO NOTHING`, [T, repo.repo_id, GHR, INS]);

  let headSeq = 0;
  const mkScenario = async ({ testCmd }) => {
    // 每场景一个真实（空树）提交——head_sha 必须是 worker 可 git fetch 的真 SHA
    git('commit', '-q', '--allow-empty', '-m', `scenario-${++headSeq}`);
    const head = git('rev-parse', 'HEAD').trim();
    await store.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: PRN, headSha: head });
    const { run } = await orch.createRunIfAbsent(pool, { tenantId: T, repoId: repo.repo_id,
      prId: (await pool.query(`SELECT pr_id FROM mu.pull_request WHERE tenant_id=$1 AND repo_id=$2 AND provider_pr_number=$3 AND head_sha=$4`,
        [T, repo.repo_id, PRN, head])).rows[0].pr_id, headSha: head });
    for (const [f, t] of [['RECEIVED', 'REVIEW_QUEUED'], ['REVIEW_QUEUED', 'REVIEWING'], ['REVIEWING', 'REVIEWED'], ['REVIEWED', 'FIX_QUEUED']]) {
      await orch.transitionRun(pool, { runId: run.run_id, from: [f], to: t });
    }
    // reviewer attempt + P0 finding（供 fixer 取数）
    const att = await orch.claimNextAttempt(pool, { runId: run.run_id, agentRole: 'reviewer',
      provider: 'deterministic', maxAttempts: 3, tenantId: T, repoId: repo.repo_id,
      prId: run.pr_id, headSha: head });
    await orch.insertFindings(pool, { attemptId: att.attemptId, runId: run.run_id,
      tenantId: T, repoId: repo.repo_id, prId: run.pr_id, headSha: head,
      findings: [{ rule_id: 'R-SECRET', severity: 'P0', confidence: 0.9, path: 'src.js',
        line_start: 2, line_end: 2, title: '凭据', evidence_ref: 'diff:src.js#L2' }] });
    return { run, binding: { tenantId: T, repoId: repo.repo_id, prId: run.pr_id, headSha: head }, testCmd };
  };

  // mock provider：返回含 secret 行的 diff（fixer 从中取 raw 行）
  __setGhProviderForTests({ async fetchPrContext(_cfg, { expectedHeadSha }) {
    return { stale_head: false, pr: { number: PRN, state: 'open', head: { sha: expectedHeadSha },
      base: { ref: 'main' }, changed_files: 1 }, diff: DIFF, checks: [],
      protection: { configured: true }, limits: { diff_bytes: 1, over_diff_limit: false },
      fetched_head_sha: expectedHeadSha };
  } });

  const deps = (testCmd) => ({ repoUrl: REPO_DIR, testCmd, providerCfg: { configured: true },
    installationId: String(INS), owner: 'prd', repoName: 'repo', prNumber: PRN,
    // internal executor requires an explicit development/test/emergency allow
    env: { MU_EXECUTOR: 'internal', MU_EXECUTOR_INTERNAL_ALLOW: 'test' },
    assertServiceChain: async () => true });

  // ── D1 Fixer dry-run → Verifier PASS → COMPLETED ──
  const s1 = await mkScenario({ testCmd: 'node -e process.exit(0)' });
  const r1 = await fxo.fixVerifyRound(pool, { run: s1.run, binding: s1.binding, deps: deps(s1.testCmd) });
  const run1 = await orch.getRun(pool, s1.run.run_id);
  const fix1 = await pool.query(`SELECT status, patch_digest FROM mu.fix_attempt WHERE run_id=$1`, [s1.run.run_id]);
  const ver1 = await pool.query(`SELECT verdict, error_code FROM mu.verification_attempt WHERE run_id=$1`, [s1.run.run_id]);
  ok('D1 fixer dry-run 产出 patch（DRY_RUN+digest）', fix1.rows[0]?.status === 'DRY_RUN' && /^[0-9a-f]{64}$/.test(fix1.rows[0]?.patch_digest ?? ''));
  ok('D1b verifier 独立验证 PASS', ver1.rows[0]?.verdict === 'PASS', ver1.rows);
  ok('D1c leader 终裁 COMPLETED', r1.ok === true && run1.status === 'COMPLETED');
  const fixAtts = await pool.query(`SELECT count(*) c FROM mu.agent_attempt WHERE run_id=$1 AND agent_role='fixer'`, [s1.run.run_id]);
  const verAtts = await pool.query(`SELECT count(*) c FROM mu.agent_attempt WHERE run_id=$1 AND agent_role='verifier'`, [s1.run.run_id]);
  ok('D1d fixer/verifier 各有真实执行 attempt', Number(fixAtts.rows[0].c) >= 1 && Number(verAtts.rows[0].c) >= 1);

  // ── D2 Verifier FAIL → REWORK → 回派第二轮 ──
  const s2 = await mkScenario({ testCmd: 'node -e process.exit(1)' });
  const r2 = await fxo.fixVerifyRound(pool, { run: s2.run, binding: s2.binding, deps: deps(s2.testCmd) });
  const run2 = await orch.getRun(pool, s2.run.run_id);
  ok('D2 verifier FAIL → REWORK_REQUIRED', run2.status === 'REWORK_REQUIRED' && r2.verdict === 'FAIL');
  const rq = await orch.transitionRun(pool, { runId: s2.run.run_id, from: ['REWORK_REQUIRED'], to: 'FIX_QUEUED' });
  ok('D2b leader 回派 FIX_QUEUED', rq.ok === true);

  // ── D3 超限 → BLOCKED + 死信（第二轮仍 FAIL）──
  const r2b = await fxo.fixVerifyRound(pool, { run: await orch.getRun(pool, s2.run.run_id), binding: s2.binding, deps: deps(s2.testCmd) });
  const run2b = await orch.getRun(pool, s2.run.run_id);
  const dl2 = await pool.query(`SELECT kind FROM mu.dead_letter WHERE run_id=$1`, [s2.run.run_id]);
  ok('D3 超限 → BLOCKED + fix_rounds_exhausted 死信', run2b.status === 'BLOCKED'
    && dl2.rows[0]?.kind === 'fix_rounds_exhausted');
  const fixCnt = await pool.query(`SELECT count(*) c FROM mu.fix_attempt WHERE run_id=$1`, [s2.run.run_id]);
  ok('D3b 有界（恰好 2 轮 fix_attempt）', Number(fixCnt.rows[0].c) === 2);

  // ── D4 crash/restart 一致性：中途状态持久 ──
  const s4 = await mkScenario({ testCmd: 'node -e process.exit(0)' });
  await orch.transitionRun(pool, { runId: s4.run.run_id, from: ['FIX_QUEUED'], to: 'FIXING' });
  // crash 模拟：另开独立池视角（服务重启后的新连接），共享池不销毁供后续场景使用
  const pool2 = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
  const run4 = await orch.getRun(pool2, s4.run.run_id);
  ok('D4 restart 后中间状态持久（FIXING）', run4.status === 'FIXING');
  const attCnt = await pool2.query(`SELECT count(*) c FROM mu.agent_attempt WHERE run_id=$1`, [s4.run.run_id]);
  ok('D4b restart 后零重复 attempt', Number(attCnt.rows[0].c) === 1);
  await pool2.end();

  // ── D5 禁改区（.github/workflows）──
  ok('D5 workflows 路径进禁改区', adapter.isForbiddenFixZone('.github/workflows/ci.yml') === true
    && adapter.isForbiddenFixZone('src/app.js') === false);

  // ── D6 服务链撤权即拒 ──
  const pool3 = new Pool({ connectionString: `postgres://postgres:x@127.0.0.1:${PORT}/mu` });
  const s6 = await mkScenario({ testCmd: 'node -e process.exit(0)' });
  const r6 = await fxo.fixVerifyRound(pool3, { run: s6.run, binding: s6.binding,
    deps: { ...deps('node -e process.exit(0)'), assertServiceChain: async () => false } });
  ok('D6 服务链失效即拒（不 spawn worker）', r6.ok === false && r6.reason === 'service_chain_invalid');

  // ── D7 无 GitHub 写（worker 只对本地 repo_url fetch；源码无 github.com 写调用）──
  const src = fs.readFileSync(path.join(here, '../lib/multiuser/agents/fxv-adapter.mjs'), 'utf8');
  ok('D7 adapter 零 GitHub API 调用（纯本地子进程契约）', !/api\.github\.com/.test(src));
} catch (e) { fail++; console.error('HARNESS ERROR', e); }
finally {
  __resetGhProvider();
  try { const p = global.POOL ?? pool; await p.end().catch(() => {}); } catch { /* */ }
  try { fs.rmSync(REPO_DIR, { recursive: true, force: true }); } catch { /* */ }
  try { execFileSync('docker', ['rm', '-f', '-v', CTR], { stdio: 'pipe' }); } catch { /* */ }
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
