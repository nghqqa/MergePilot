#!/usr/bin/env node
// rag-worker-snapshot.integration.mjs — RAG V2 worker 执行期快照 E2E（#491）。
// 自起一次性 pgvector 容器（随机回环端口，finally 强制清理）；mock GitHub provider
// 零真实网络、零真实凭据；语料为合成测试文档（不导入任何生产语料）。
//
// 全链：真实 handlePullRequestEvent（review-service 审查链）→ worker-rag 执行期
// 检索 → run_rag_snapshots 落库 → 快照读取追溯。覆盖：
//   W1-W3  worker 证据：审查链内真实产生快照（任务身份服务端同源+审计+决策留痕）
//   W4     重试幂等（重复触发+直调重试均首写即定，不覆盖）
//   W5     语料更新后旧快照不变；新 run 检索到新语料
//   W6     快照读取授权（同租户+授权面=200；异租户/repo 越权=403；无快照=未记录）
//   W7     模型不可用：快照如实记录 model_missing（不伪装命中，审查不受阻）
//   W8     保存失败：审查不受阻、无快照行（未留存）、query_log 审计、决策 not_saved
//   W9-W10 获取关闭/模型未配置：不落任何行（attempted=false，无决策伪造）
// 运行：node console/backend/test/rag-worker-snapshot.integration.mjs
import { execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const { Pool } = createRequire(path.join(here, 'support/noop.js'))('pg');
const { createMuStore } = await import('../lib/multiuser/store.mjs');
const svc = await import('../lib/multiuser/review-service.mjs');
const { __setGhProviderForTests, __resetGhProvider } = await import('../lib/multiuser/ghprovider.mjs');
const { createRagTrialStore } = await import('../lib/ragtrial/store.mjs');
const { maybeSnapshotRunReferences } = await import('../lib/ragtrial/worker-rag.mjs');
const { ragTrialApi } = await import('../lib/ragtrial/api.mjs');

let pass = 0, fail = 0;
const ok = (n, c, d) => { c ? (pass++, console.log('  PASS  ' + n)) : (fail++, console.log('  FAIL  ' + n + (d ? ' — ' + JSON.stringify(d).slice(0, 220) : ''))); };

const DIFF_DIRTY = `diff --git a/src/db.js b/src/db.js
index 111..222 100644
--- a/src/db.js
+++ b/src/db.js
@@ -1,2 +1,4 @@
 const base = 1;
+const q = "SELECT * FROM users WHERE id=" + userId;
+const PAT = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij";
+require('child_process').execSync("rm -rf " + dir);`;
const DIFF_CLEAN = `diff --git a/README.md b/README.md
index bbb..ccc 100644
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # doc
+说明文档补充。`;

// ── 一次性 pgvector 容器 ──
const CTR = `ragwkr-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 16200 + Math.floor(Math.random() * 200);
const PG_PASSWORD = 'ragwkr-it-only';
let dsn = process.env.RAG_SNAPSHOT_TEST_DSN || null;
let pool;
if (!dsn) {
  execFileSync('docker', ['run', '-d', '--name', CTR,
    '-e', `POSTGRES_PASSWORD=${PG_PASSWORD}`, '-e', 'POSTGRES_DB=mu',
    '-p', `127.0.0.1:${PORT}:5432`, 'pgvector/pgvector:pg16'], { stdio: 'pipe' });
  dsn = `postgres://postgres:${PG_PASSWORD}@127.0.0.1:${PORT}/mu`;
}
const deadline = Date.now() + 60_000;
for (;;) {
  try { pool = new Pool({ connectionString: dsn, max: 6 }); await pool.query('SELECT 1'); break; }
  catch { if (Date.now() > deadline) throw new Error('pg readiness timeout'); await new Promise((r) => setTimeout(r, 700)); }
}

try {
  // ── mu 侧种子（与 mu-review-prb 同构：binding 行提供 owner/name 服务端真源） ──
  const muStore = await createMuStore({ pool, env: { MU_BOOTSTRAP_ADMIN_LOGIN: 'wkr-admin' } });
  await muStore.initSchema(); await muStore.bootstrap();
  const T = (await muStore.ensureTenant({ slug: 'wkr-t', displayName: 'WKR' })).tenant_id;
  const repo = await muStore.ensureRepository({ tenantId: T, provider: 'github',
    providerRepoId: '96001', owner: 'wkr', name: 'repo', defaultBranch: 'main' });
  const INS = 78001, GHR = 96001, PRN = 7;
  await pool.query(
    `INSERT INTO mu.github_app_installation (installation_id, tenant_id, account_id, account_login, app_id)
     VALUES ($1,$2,1,'wkr-owner',1) ON CONFLICT DO NOTHING`, [INS, T]);
  await pool.query(
    `INSERT INTO mu.repository_binding (tenant_id, repo_id, github_repo_id, owner, name, installation_id, default_branch, binding_state)
     VALUES ($1,$2,$3,'wkr','repo',$4,'main','active') ON CONFLICT DO NOTHING`,
    [T, repo.repo_id, GHR, INS]);

  // ── ragtrial 侧：initSchema（默认注册 local-hash-v1）+ 合成语料（repo@main） ──
  const ragEnv = { RAGTRIAL_EMBED_MODEL_ID: 'local-hash-v1', RAGTRIAL_ALLOWED_SCOPES: 'wkr/repo@main' };
  const ragStore = await createRagTrialStore({ pool, env: ragEnv });
  await ragStore.initSchema();
  const ing1 = await ragStore.ingestDocuments({ repo: 'wkr/repo', branch: 'main', docs: [
    { path: 'docs/runbook.md', text: '# 运行手册\n\n部署前必须检查回滚锚点 digest。\n\n灰度发布按批次推进，每批观察指标五分钟。\n' },
    { path: 'docs/security.md', text: '# 安全基线\n\n密钥不得入库，一律走环境变量注入。\n\nSQL 字符串拼接是注入风险，必须参数化。\n' },
    { path: 'docs/review-rules.md', text: '# 审查规则说明\n\n疑似凭据 密钥形状 shell 命令拼接 路径穿越 不安全反序列化\n\nworkflow 权限扩大 依赖 manifest 高风险变化。\n' },
  ]});
  ok('W0 前置：合成语料入库', Array.isArray(ing1.report) && ing1.report.length === 3, ing1.report);

  // ── mock provider（各 head 一个上下文；base_ref=main 与语料分支一致） ──
  const H1 = 'a1'.repeat(20), H3 = 'a3'.repeat(20), H4 = 'a4'.repeat(20),
        H5 = 'a5'.repeat(20), H6 = 'a6'.repeat(20), H7 = 'a7'.repeat(20);
  for (const h of [H1, H3, H4, H5, H6, H7]) {
    await muStore.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: PRN, headSha: h });
  }
  const mkCtx = (headSha, title, diff) => ({ stale_head: false,
    pr: { number: PRN, state: 'open', title, head: { sha: headSha, ref: 'b' },
      base: { sha: '0'.repeat(40), ref: 'main' }, changed_files: 3 },
    diff, checks: [], protection: { configured: true },
    limits: { diff_bytes: 10, over_diff_limit: false, over_file_limit: false },
    fetched_head_sha: headSha });
  __setGhProviderForTests({
    async fetchPrContext(_cfg, { expectedHeadSha, prNumber }) {
      if (expectedHeadSha === H1) return mkCtx(H1, '部署与回滚流程修复', DIFF_DIRTY);
      if (expectedHeadSha === H3) return mkCtx(H3, '索引失效与重建版本流程审查', DIFF_CLEAN);
      if (expectedHeadSha === H4) return mkCtx(H4, '模型缺失场景', DIFF_CLEAN);
      if (expectedHeadSha === H5) return mkCtx(H5, '保存失败场景', DIFF_CLEAN);
      if (expectedHeadSha === H6) return mkCtx(H6, '获取关闭场景', DIFF_CLEAN);
      return mkCtx(expectedHeadSha, '模型未配置场景', DIFF_CLEAN);
    },
  });

  // ── W1-W3：真实审查链 → worker 检索 → 快照落库 ──
  const CFG = { configured: true, llmEnv: { ...ragEnv } };
  const r1 = await svc.handlePullRequestEvent(pool, CFG, {
    payload: { action: 'opened', github_repo_id: GHR, pr_number: PRN, head_sha: H1 } });
  ok('W1 审查链完成（REVIEWED 且有发现——worker 未改变审查主链）',
    r1.ok === true && r1.run.status === 'REVIEWED' && (r1.findings_count ?? 0) >= 1, r1);
  const snap1 = (await pool.query(
    `SELECT * FROM ragtrial.run_rag_snapshots WHERE run_id=$1 AND corpus_branch='main'`, [r1.run.run_id])).rows;
  ok('W2a 快照行存在且任务身份服务端同源（run/tenant/repo 来自真实任务）',
    snap1.length === 1 && snap1[0].run_id === r1.run.run_id && snap1[0].tenant_id === T
    && snap1[0].repo === 'wkr/repo' && snap1[0].review_head === H1 && snap1[0].base_ref === 'main', snap1[0] ?? null);
  ok('W2b 检索语义齐备（模型/策略 pr_context_v1/hit/引用含 doc_path+sha）',
    snap1[0]?.model_id === 'local-hash-v1' && snap1[0]?.query_strategy === 'pr_context_v1'
    && snap1[0]?.service_state === 'hit' && (snap1[0]?.citations ?? []).length >= 1
    && !!snap1[0]?.citations[0]?.citation?.doc_path && !!snap1[0]?.citations[0]?.citation?.doc_sha256,
    snap1[0]?.citations?.length);
  const qlog1 = (await pool.query(
    `SELECT seq FROM ragtrial.query_log WHERE actor=$1 AND state='hit' LIMIT 1`,
    [`worker-review:${r1.run.run_id}`])).rows;
  ok('W3a query_log 有 worker 执行留痕（actor=worker-review:<run>）', qlog1.length === 1);
  const dec1 = (await pool.query(
    `SELECT decision, rationale_ref FROM mu.orchestration_decision
      WHERE run_id=$1 AND stage='rag_worker_reference'`, [r1.run.run_id])).rows;
  ok('W3b 决策留痕 rag_worker_reference=saved 且 rationale_ref=快照 id',
    dec1.length === 1 && dec1[0].decision === 'saved'
    && dec1[0].rationale_ref === snap1[0].snapshot_id, dec1);

  // ── W6：快照读取授权（HTTP 层，fake session；api.mjs 池经 CONSOLE_PG_DSN 解析） ──
  process.env.CONSOLE_PG_DSN = dsn;
  const httpGet = async (auth, runId) => {
    let status = null, body = null;
    const res = {};
    await ragTrialApi({ method: 'GET', headers: {}, socket: { remoteAddress: '127.0.0.1' } }, res, {
      p: `/api/rag-trial/snapshot/${runId}`, q: { corpus_branch: 'main' },
      sendJson: (_res, s, b) => { status = s; body = b; },
      readJsonBody: async () => ({}),
      requireSession: async () => auth,
    });
    return { status, body };
  };
  const authOk = { user: { name: 'wkr-maintainer' }, repos: ['wkr/repo'], _mu: { tenantId: T } };
  const g1 = await httpGet(authOk, r1.run.run_id);
  ok('W6a 同租户+授权面 → 200 snapshot（含双源元数据）',
    g1.status === 200 && g1.body?.service_state === 'snapshot'
    && g1.body?.corpus_branch === 'main' && g1.body?.query_strategy === 'pr_context_v1'
    && Array.isArray(g1.body?.citations), g1.status);
  const g2 = await httpGet({ user: { name: 'other' }, repos: ['wkr/repo'], _mu: { tenantId: '99999999-9999-9999-9999-999999999999' } }, r1.run.run_id);
  ok('W6b 异租户 → 403 run_tenant_mismatch（请求自带 run_id 不构成绑定证明）',
    g2.status === 403 && g2.body?.error?.reason === 'run_tenant_mismatch', g2);
  const g3 = await httpGet({ user: { name: 'wkr2' }, repos: ['other/repo'], _mu: { tenantId: T } }, r1.run.run_id);
  ok('W6c 同租户但 repo 越权 → 403 repo_not_in_allowlist',
    g3.status === 403 && g3.body?.error?.reason === 'repo_not_in_allowlist', g3);
  const g4 = await httpGet(authOk, '88888888-8888-8888-8888-888888888888');
  ok('W6d 无快照 run → 200 no_snapshot（未记录，不补造）',
    g4.status === 200 && g4.body?.service_state === 'no_snapshot', g4);

  // ── W4：重试幂等（重复触发 + 直调重试均不覆盖） ──
  const beforeAt = snap1[0].snapshot_at.getTime();
  const r1b = await svc.handlePullRequestEvent(pool, CFG, {
    payload: { action: 'synchronize', github_repo_id: GHR, pr_number: PRN, head_sha: H1 } });
  const snapCnt = (await pool.query(
    `SELECT count(*)::int c, max(snapshot_at) at FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r1.run.run_id])).rows[0];
  ok('W4a 重复触发幂等返回且快照仍单条、时间戳不变（首写即定）',
    r1b.ok === true && r1b.idempotent === true && snapCnt.c === 1
    && new Date(snapCnt.at).getTime() === beforeAt, snapCnt);
  const retry = await maybeSnapshotRunReferences(pool, CFG.llmEnv, {
    runId: r1.run.run_id, tenantId: T, repo: 'wkr/repo', baseRef: 'main',
    reviewHead: H1, prTitle: '部署与回滚流程修复', findingTitles: ['篡改过的标题——不得覆盖已保存快照'] });
  ok('W4b 直调重试 saved=false 且返回既有快照（citations 未被改写）',
    retry.attempted === true && retry.saved === false
    && JSON.stringify(retry.snapshot?.citations) === JSON.stringify(snap1[0].citations), retry.saved);

  // ── W5：语料更新 → 旧快照不变；新 run 检索到新语料 ──
  await ragStore.ingestDocuments({ repo: 'wkr/repo', branch: 'main', docs: [
    { path: 'docs/runbook.md', text: '# 运行手册\n\n部署前必须检查回滚锚点 digest。\n\n索引失效后必须重建版本再查询。回滚窗口保留最近两个版本。\n' },
    { path: 'docs/index-maintenance.md', text: '# 索引维护\n\n索引失效必须重建版本；灰度期间旧版本保留供回滚。\n' },
  ]});
  const reread1 = (await pool.query(
    `SELECT citations, snapshot_at FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r1.run.run_id])).rows[0];
  ok('W5a 语料更新后旧 run 快照逐字节不变',
    JSON.stringify(reread1.citations) === JSON.stringify(snap1[0].citations)
    && reread1.snapshot_at.getTime() === beforeAt);
  const r3 = await svc.handlePullRequestEvent(pool, CFG, {
    payload: { action: 'synchronize', github_repo_id: GHR, pr_number: PRN, head_sha: H3 } });
  const snap3 = (await pool.query(
    `SELECT * FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r3.run.run_id])).rows[0];
  ok('W5b 新 run 快照检索到更新后语料（含新文档且引用新内容）',
    r3.ok === true && r3.run.status === 'REVIEWED'
    && !!snap3 && (snap3.citations ?? []).some((c) => c.citation?.doc_path === 'docs/index-maintenance.md'),
    snap3?.citations?.map((c) => c.citation?.doc_path));

  // ── W7：模型不可用 → 快照如实记录 model_missing（审查不受阻） ──
  const r4 = await svc.handlePullRequestEvent(pool, { configured: true,
    llmEnv: { RAGTRIAL_EMBED_MODEL_ID: 'no-such-model', RAGTRIAL_ALLOWED_SCOPES: 'wkr/repo@main' } }, {
    payload: { action: 'synchronize', github_repo_id: GHR, pr_number: PRN, head_sha: H4 } });
  const snap4 = (await pool.query(
    `SELECT * FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r4.run.run_id])).rows[0];
  ok('W7 模型缺失：审查完成 + 快照如实 model_missing（citations=[]，不伪装命中）',
    r4.ok === true && r4.run.status === 'REVIEWED'
    && snap4?.service_state === 'model_missing' && (snap4?.citations ?? []).length === 0, snap4?.service_state);

  // ── W8：保存失败 → 审查不受阻、无快照行、审计留痕、决策 not_saved ──
  // 破坏手法=BEFORE INSERT 触发器抛错（F3 自愈后 DROP TABLE 会被 worker 首建
  // initSchema 重建——触发器不可自愈，仍能命中保存失败路径）。
  await pool.query(`CREATE FUNCTION rag_wkr_kill() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'snapshot save sabotaged for W8'; END $$ LANGUAGE plpgsql`);
  await pool.query(`CREATE TRIGGER rag_wkr_kill_ins BEFORE INSERT ON ragtrial.run_rag_snapshots
    FOR EACH ROW EXECUTE FUNCTION rag_wkr_kill()`);
  const r5 = await svc.handlePullRequestEvent(pool, CFG, {
    payload: { action: 'synchronize', github_repo_id: GHR, pr_number: PRN, head_sha: H5 } });
  await pool.query(`DROP TRIGGER rag_wkr_kill_ins ON ragtrial.run_rag_snapshots`);
  await pool.query(`DROP FUNCTION rag_wkr_kill()`);
  const saveFailLog = (await pool.query(
    `SELECT seq FROM ragtrial.audit_events WHERE kind='RAG_WORKER_SAVE_FAILED' ORDER BY seq DESC LIMIT 1`)).rows;
  const dec5 = (await pool.query(
    `SELECT decision FROM mu.orchestration_decision WHERE run_id=$1 AND stage='rag_worker_reference'`,
    [r5.run.run_id])).rows;
  ok('W8 保存失败：审查 REVIEWED + 审计留痕 + 决策 not_saved:*（不伪造已留存）',
    r5.ok === true && r5.run.status === 'REVIEWED' && saveFailLog.length === 1
    && dec5.length === 1 && String(dec5[0].decision).startsWith('not_saved:'),
    { reviewed: r5.run?.status, logs: saveFailLog.length, dec: dec5[0]?.decision });
  const h5rows = (await pool.query(
    `SELECT count(*)::int c FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r5.run.run_id])).rows[0].c;
  ok('W8b H5 无任何快照行（保存失败=未留存，非延迟写入）', h5rows === 0);

  // ── W9：获取关闭（RAGTRIAL_WORKER_RAG=off）→ 不落任何行、无决策 ──
  const r6 = await svc.handlePullRequestEvent(pool, { configured: true,
    llmEnv: { ...ragEnv, RAGTRIAL_WORKER_RAG: 'off' } }, {
    payload: { action: 'synchronize', github_repo_id: GHR, pr_number: PRN, head_sha: H6 } });
  const offRows = (await pool.query(
    `SELECT count(*)::int c FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r6.run.run_id])).rows[0].c;
  const offDec = (await pool.query(
    `SELECT count(*)::int c FROM mu.orchestration_decision WHERE run_id=$1 AND stage='rag_worker_reference'`,
    [r6.run.run_id])).rows[0].c;
  ok('W9 获取关闭：审查完成且零快照行、零 rag 决策（attempted=false 如实无痕）',
    r6.ok === true && r6.run.status === 'REVIEWED' && offRows === 0 && offDec === 0);

  // ── W10：模型未配置 → 不检索不落行，query_log 有 RAG_WORKER_SKIP ──
  const r7 = await svc.handlePullRequestEvent(pool, { configured: true,
    llmEnv: { RAGTRIAL_ALLOWED_SCOPES: 'wkr/repo@main' } }, {
    payload: { action: 'synchronize', github_repo_id: GHR, pr_number: PRN, head_sha: H7 } });
  const skipLog = (await pool.query(
    `SELECT detail FROM ragtrial.audit_events WHERE kind='RAG_WORKER_SKIP' ORDER BY seq DESC LIMIT 1`)).rows;
  const skipRows = (await pool.query(
    `SELECT count(*)::int c FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r7.run.run_id])).rows[0].c;
  ok('W10 模型未配置：零快照行 + RAG_WORKER_SKIP(model_unresolved) 审计',
    r7.ok === true && r7.run.status === 'REVIEWED' && skipRows === 0
    && skipLog.length === 1 && skipLog[0].detail?.reason === 'model_unresolved',
    skipLog[0]);

  // ── W11（独立审阅 F1 修正回归）：scope 边界——内部调用不绕过部署 scope 门 ──
  const H8 = 'a8'.repeat(20);
  await muStore.upsertPullRequest({ tenantId: T, repoId: repo.repo_id, providerPrNumber: PRN, headSha: H8 });
  const r8 = await svc.handlePullRequestEvent(pool, { configured: true,
    llmEnv: { ...ragEnv, RAGTRIAL_ALLOWED_SCOPES: 'other/repo@main' } }, {
    payload: { action: 'synchronize', github_repo_id: GHR, pr_number: PRN, head_sha: H8 } });
  const h8rows = (await pool.query(
    `SELECT count(*)::int c FROM ragtrial.run_rag_snapshots WHERE run_id=$1`, [r8.run.run_id])).rows[0].c;
  const h8dec = (await pool.query(
    `SELECT count(*)::int c FROM mu.orchestration_decision WHERE run_id=$1 AND stage='rag_worker_reference'`,
    [r8.run.run_id])).rows[0].c;
  ok('W11a scope 不含 repo：审查完成且零快照零决策（内部调用不绕过 scope 门）',
    r8.ok === true && r8.run.status === 'REVIEWED' && h8rows === 0 && h8dec === 0,
    { reviewed: r8.run?.status, rows: h8rows, dec: h8dec });
  const scopeAudit = (await pool.query(
    `SELECT detail FROM ragtrial.audit_events WHERE kind='RAG_WORKER_SKIP' ORDER BY seq DESC LIMIT 1`)).rows;
  ok('W11b scope 拒绝有界审计（reason=scope_not_allowed）',
    scopeAudit.length === 1 && scopeAudit[0].detail?.reason === 'scope_not_allowed', scopeAudit[0]);
  const r8b = await maybeSnapshotRunReferences(pool, { RAGTRIAL_EMBED_MODEL_ID: 'local-hash-v1' }, {
    runId: r8.run.run_id, tenantId: T, repo: 'wkr/repo', baseRef: 'main',
    reviewHead: H8, prTitle: 'x', findingTitles: [] });
  ok('W11c scope 未配置 → 默认拒绝跳过（attempted=false, scope_not_configured）',
    r8b.attempted === false && r8b.reason === 'scope_not_configured', r8b);

  console.log(`\nrag-worker-snapshot.integration: ${pass} passed, ${fail} failed`);
  process.exitCode = fail ? 1 : 0;
} finally {
  __resetGhProvider();
  await pool.end().catch(() => {});
  if (!process.env.RAG_SNAPSHOT_TEST_DSN) {
    try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* 已清理 */ }
  }
}
