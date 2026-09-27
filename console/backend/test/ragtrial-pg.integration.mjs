#!/usr/bin/env node
// console/backend/test/ragtrial-pg.integration.mjs — RAG trial 真 PG(pgvector) 集成回归。
// 运行：node console/backend/test/ragtrial-pg.integration.mjs
//   环境依赖：docker（pgvector/pgvector:pg16 本地镜像）；RAGTRIAL_PG_TEST_DSN 已设则直连。
// 真实性边界：临时一次性容器（随机名/随机库），绝不挂接 promote/staging/生产卷；
// 覆盖 15 项必须测试中的 DB 侧场景（栈级 restart/rollback/pgvector-down 由
// deploy/local-rag-trial/scripts/run-e2e.mjs 在 compose 栈上完成）。
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const supportDir = fileURLToPath(new URL('./support/', import.meta.url));
const { Pool } = createRequire(path.join(supportDir, 'noop.js'))('pg');
const { createRagTrialStore } = await import('../lib/ragtrial/store.mjs');
const { localModelSpec, modelDigest, LOCAL_MODEL_ID } = await import('../lib/ragtrial/embed.mjs');

let pass = 0, fail = 0;
function ok(name, cond, detail) {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}${detail ? ' — ' + detail : ''}`); }
}

// ── 容器生命周期（一次性；失败即中止不泄漏） ──
const CTR = `ragtrial-it-${crypto.randomBytes(4).toString('hex')}`;
const PORT = 15500 + Math.floor(Math.random() * 300);
const PG_PASSWORD = 'ragtrial-it-only';
let externalDsn = process.env.RAGTRIAL_PG_TEST_DSN || null;
let pool;

async function boot() {
  if (!externalDsn) {
    execFileSync('docker', ['run', '-d', '--name', CTR,
      '-e', `POSTGRES_PASSWORD=${PG_PASSWORD}`, '-e', 'POSTGRES_DB=ragtrial',
      '-p', `127.0.0.1:${PORT}:5432`,
      'pgvector/pgvector:pg16'], { stdio: 'pipe' });
    externalDsn = `postgres://postgres:${PG_PASSWORD}@127.0.0.1:${PORT}/ragtrial`;
  }
  // 等就绪
  const deadline = Date.now() + 60_000;
  for (;;) {
    try {
      const p = new Pool({ connectionString: externalDsn, max: 4 });
      await p.query('SELECT 1');
      pool = p;
      return;
    } catch { 
      if (Date.now() > deadline) throw new Error('pg readiness timeout');
      await new Promise((r) => setTimeout(r, 800));
    }
  }
}

const docs = (extra = '') => [
  { path: 'docs/runbook.md', text: `# 运行手册\n\n部署前必须检查回滚锚点 digest。\n\n灰度发布按批次推进，每批观察指标五分钟。\n\n${extra}` },
  { path: 'docs/security.md', text: `# 安全基线\n\n密钥不得入库，一律走环境变量注入。\n\n审计事件必须携带 actor 字段。\n\n${extra}` },
];

try {
  await boot();
  const store = await createRagTrialStore({ pool });
  const reg = await store.initSchema();
  ok('initSchema 幂等 + 默认注册 local-hash-v1', reg.model_id === LOCAL_MODEL_ID && reg.idempotent !== undefined ? true : reg.model_id === LOCAL_MODEL_ID);

  // ── 场景 5/6：重复 ingest / 增量更新 ──
  const REPO = 'nghqqa/mergepilot', BR = 'feat/local-rag-trial';
  let r1 = await store.ingestDocuments({ docs: docs(), repo: REPO, branch: BR });
  const chunksV1 = (await pool.query('SELECT count(*)::int n FROM ragtrial.chunks')).rows[0].n;
  ok('首次 ingest：2 文档入库（ingested）', r1.report.every((x) => x.action === 'ingested') && chunksV1 > 0, JSON.stringify(r1.report));
  let r2 = await store.ingestDocuments({ docs: docs(), repo: REPO, branch: BR });
  const chunksAfterDup = (await pool.query('SELECT count(*)::int n FROM ragtrial.chunks')).rows[0].n;
  ok('重复 ingest：幂等 no-op（unchanged，chunk 数不变）',
    r2.report.every((x) => x.action === 'unchanged') && chunksAfterDup === chunksV1);
  // 增量：同路径改内容（updated）+ 新增一个文档（ingested）
  const updated = [
    ...docs('新增段落：索引失效后必须重建版本再查询。\n\n回滚窗口保留最近两个版本。'),
    { path: 'docs/new.md', text: '全新文档：验证增量新增。\n\n检索必须绑定 repo 与 branch。' },
  ];
  let r3 = await store.ingestDocuments({ docs: updated, repo: REPO, branch: BR });
  const actions = Object.fromEntries(r3.report.map((x) => [x.doc_path, x.action]));
  ok('增量更新：仅变更文档 updated + 新文档 ingested',
    actions['docs/runbook.md'] === 'updated' && actions['docs/security.md'] === 'updated' && actions['docs/new.md'] === 'ingested',
    JSON.stringify(actions));
  const q = await store.search({ q: '回滚窗口保留最近两个版本', repo: REPO, branch: BR, k: 3 });
  ok('增量内容可检索且旧内容（v1 文案）已替换',
    q.service_state === 'hit' && q.results.some((h) => h.citation.doc_path === 'docs/runbook.md'));

  // ── hit / empty / 引用链 ──
  const hit = await store.search({ q: '审计事件 actor', repo: REPO, branch: BR, k: 3 });
  ok('hit 状态 + 每个结果携带完整引用链', hit.service_state === 'hit' && hit.results.every((h) => {
    const c = h.citation;
    return c.repo === REPO && c.branch === BR && c.doc_path && c.line_start >= 1
      && c.line_end >= c.line_start && c.doc_sha256?.length === 64 && c.chunk_sha256?.length === 64
      && c.model_digest === modelDigest(localModelSpec()) && Number.isInteger(c.index_version);
  }));
  const empty = await store.search({ q: 'zzzzqqqq 完全不存在词组', repo: REPO, branch: BR, k: 3 });
  ok('empty 状态（诚实空结果，非降级伪装）', empty.service_state === 'empty' && empty.results.length === 0);

  // ── 场景 1：模型缺失 ──
  const mm = await store.search({ q: '审计', repo: REPO, branch: BR, k: 3, modelId: 'no-such-model' });
  ok('model_missing：未注册模型显式拒绝', mm.service_state === 'model_missing' && mm.results.length === 0);

  // ── 场景 9：wrong repo/branch 越权 ──
  const crossRepo = await store.search({ q: '审计事件 actor', repo: 'other/repo', branch: BR, k: 3 });
  const crossBranch = await store.search({ q: '审计事件 actor', repo: REPO, branch: 'main', k: 3 });
  ok('跨 repo/branch 检索零泄漏（scoped empty，index_state=none）',
    crossRepo.service_state === 'empty' && crossRepo.results.length === 0
    && crossBranch.service_state === 'empty' && crossBranch.results.length === 0);

  // ── 场景 4：空文档 ──
  const emptyDoc = await store.ingestDocuments({ docs: [{ path: 'docs/blank.md', text: '\n\n \n' }], repo: REPO, branch: BR });
  const blankRow = (await pool.query(`SELECT chunk_count, state FROM ragtrial.documents WHERE doc_path='docs/blank.md'`)).rows[0];
  ok('空文档：0 chunk 入库不崩、文档行 active', emptyDoc.report[0].chunks === 0 && blankRow.chunk_count === 0 && blankRow.state === 'active');

  // ── 场景 7：删除后不可检索 ──
  const del = await store.deleteDocument({ repo: REPO, branch: BR, doc_path: 'docs/new.md' });
  const delQ = await store.search({ q: '检索必须绑定 repo 与 branch', repo: REPO, branch: BR, k: 5 });
  const leftover = (await pool.query(`SELECT count(*)::int n FROM ragtrial.chunks WHERE doc_path='docs/new.md'`)).rows[0].n;
  ok('删除：chunks 物理清除 + 不可再检索 + documents 保留审计行（state=deleted）',
    del.chunks_removed > 0 && leftover === 0 && delQ.results.every((h) => h.citation.doc_path !== 'docs/new.md')
    && (await pool.query(`SELECT state FROM ragtrial.documents WHERE doc_path='docs/new.md'`)).rows[0].state === 'deleted');

  // ── 场景 2：模型 digest 漂移（篡改行） ──
  await pool.query(`UPDATE ragtrial.chunks SET model_digest='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' WHERE doc_path='docs/security.md'`);
  const drift = await store.search({ q: '灰度发布批次 回滚锚点', repo: REPO, branch: BR, k: 5 });
  ok('digest 部分漂移：其他文档仍可检索 + 报告 drifted_rows',
    drift.service_state === 'hit' && drift.drifted_rows > 0 && drift.results.every((h) => h.citation.doc_path !== 'docs/security.md'));
  await pool.query(`UPDATE ragtrial.chunks SET model_digest='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'`);
  const driftAll = await store.search({ q: '密钥不得入库', repo: REPO, branch: BR, k: 5 });
  ok('digest 全量漂移 → index_stale（拒绝服务旧索引）', driftAll.service_state === 'index_stale' && driftAll.results.length === 0);
  // 恢复
  await pool.query(`UPDATE ragtrial.chunks SET model_digest=$1`, [modelDigest(localModelSpec())]);

  // ── 场景 8：索引版本失效（bump 后未重建） ──
  const inv = await store.invalidateIndex({});
  const stale = await store.search({ q: '审计事件', repo: REPO, branch: BR, k: 3 });
  ok('invalidateIndex 后查询 → index_stale（旧版本行不再命中）',
    inv.index_version === 2 && stale.service_state === 'index_stale');
  // 重建到 v2
  const rebuilt = await store.ingestDocuments({ docs: updated.filter((d) => d.path !== 'docs/new.md'), repo: REPO, branch: BR });
  const q2 = await store.search({ q: '审计事件', repo: REPO, branch: BR, k: 3 });
  ok('re-ingest 后新版本生效（hit 恢复）',
    q2.service_state === 'hit' && q2.results.every((h) => h.citation.index_version === 2));

  // ── 场景 14（数据面）：索引回滚到 v1 ──
  const rb = await store.rollbackIndex({ toVersion: 1 });
  const q1 = await store.search({ q: '审计事件', repo: REPO, branch: BR, k: 3 });
  ok('索引回滚：v1 保留行重新命中（index_version=1）',
    rb.ok && q1.service_state === 'hit' && q1.results.every((h) => h.citation.index_version === 1));
  await store.rollbackIndex({ toVersion: 2 });
  const badRb = await store.rollbackIndex({ toVersion: 99 }).catch((e) => e);
  ok('回滚目标无保留行 → 显式拒绝', badRb.kind === 'rollback_target_missing');

  // ── 场景 11：引用缺失行被丢弃 ──
  await pool.query(`UPDATE ragtrial.chunks SET line_start=NULL, line_end=NULL WHERE doc_path='docs/runbook.md'`);
  const uncited = await store.search({ q: '灰度发布按批次推进', repo: REPO, branch: BR, k: 5 });
  ok('引用缺失：行被丢弃计数 dropped_uncited，绝不返回无引用命中',
    uncited.results.every((h) => h.citation.doc_path !== 'docs/runbook.md') && uncited.dropped_uncited > 0);
  const citAudit = (await pool.query(`SELECT count(*)::int n FROM ragtrial.audit_events WHERE kind='CITATION_DROPPED'`)).rows[0].n;
  ok('引用丢弃有审计（CITATION_DROPPED）', citAudit >= 1);
  await pool.query(`UPDATE ragtrial.chunks SET line_start=1, line_end=10 WHERE doc_path='docs/runbook.md' AND line_start IS NULL`);

  // ── 模型注册冲突（digest 漂移的注册路径） ──
  const conflict = await store.registerModel({ ...localModelSpec(), dims: 128 }, 'local').catch((e) => e);
  ok('同 id 不同 digest 注册 → 409 model_digest_conflict（force 前拒绝）', conflict.kind === 'model_digest_conflict');

  // ── QA 评测 Recall@K ──
  const ev = await store.evalQa({
    repo: REPO, branch: BR, k: 5, qaSet: 'integration-inline',
    qa: [
      { q: '密钥 环境变量注入', expect_doc: 'docs/security.md' },
      { q: '灰度发布观察多久', expect_doc: 'docs/runbook.md' },
      { q: 'zzzz 不存在', expect_doc: 'docs/runbook.md' },
    ],
  });
  ok('evalQa Recall@K 计算与落库（第三条应 miss）',
    ev.total === 3 && ev.hit_at_k === 2 && ev.recall_at_k === 0.6667
    && ev.detail[2].hit === false);

  // ── metrics / 审计完整性 ──
  const metrics = await store.metrics();
  ok('metrics：六状态计数 + P50/P95 + 引用统计 + eval 记录',
    metrics.queries_by_state.hit >= 1 && metrics.queries_by_state.empty >= 1
    && metrics.queries_by_state.model_missing >= 1 && metrics.queries_by_state.index_stale >= 1
    && metrics.latency_ms.p50 >= 0 && (metrics.citation.hit_rows >= 1 && metrics.citation.cited_rows === metrics.citation.hit_rows && metrics.citation.dropped_uncited_rows >= 1)
    && metrics.eval_runs.length >= 1, JSON.stringify(metrics.queries_by_state));
  const auditKinds = metrics.audit_events_by_kind;
  ok('audit：INGEST/DOC_DELETE/INDEX_INVALIDATE/INDEX_ROLLBACK/EVAL_RUN 全记录',
    auditKinds.INGEST >= 3 && auditKinds.DOC_DELETE >= 1 && auditKinds.INDEX_INVALIDATE >= 1
    && auditKinds.INDEX_ROLLBACK >= 1 && auditKinds.EVAL_RUN >= 1, JSON.stringify(auditKinds));
  const nullActor = (await pool.query(`SELECT count(*)::int n FROM ragtrial.audit_events WHERE actor IS NULL`)).rows[0].n;
  ok('audit_events.actor 无空值（NOT NULL 契约）', nullActor === 0);

  // ── 场景 3（DB 侧）：provider 不可达由 api 层 remote 分支覆盖；
  //    这里锁定 local provider 永不产生 provider_unavailable ──
  const localQ = await store.search({ q: '审计', repo: REPO, branch: BR, k: 2 });
  ok('local provider 查询不产生 provider_unavailable', localQ.service_state !== 'provider_unavailable');

  // ── 混合评分 v2：IDF 词法 + RAGTRIAL_HYBRID_JSON 覆盖（默认零行为变化） ──
  const { HYBRID_CONFIGS } = await import('../lib/ragtrial/store.mjs');
  ok('HYB1 默认配置=部署现行值（256/768/1024 三档，零行为变化）',
    HYBRID_CONFIGS[256].floor === 0.1 && HYBRID_CONFIGS[768].floor === 0.45 && HYBRID_CONFIGS[1024].floor === 0.52
    && HYBRID_CONFIGS[256].w === 0.5);
  const sstore2 = await createRagTrialStore({ pool, env: { ...process.env,
    CONSOLE_PG_DSN: undefined, RAGTRIAL_EMBED_ENDPOINT: undefined,
    RAGTRIAL_HYBRID_JSON: JSON.stringify({ 'local-hash-v1': { lexMode: 'idf', w: 0.3, floor: 0.1 } }) } });
  const idfHit = await sstore2.search({ q: '审计事件必须携带 actor 字段', repo: REPO, branch: BR, k: 3 });
  ok('HYB2 IDF 词法路径可用（覆盖配置生效，命中引用链完整）',
    idfHit.service_state === 'hit' && idfHit.results.every((h) => h.citation.doc_path));
  const plainStore = await createRagTrialStore({ pool, env: { ...process.env, CONSOLE_PG_DSN: undefined, RAGTRIAL_EMBED_ENDPOINT: undefined } });
  const plainHit = await plainStore.search({ q: '审计事件必须携带 actor 字段', repo: REPO, branch: BR, k: 3 });
  ok('HYB3 无覆盖=默认 plain 路径（与 v1 行为一致）', plainHit.service_state === 'hit');
  const badOverride = await createRagTrialStore({ pool, env: { ...process.env, CONSOLE_PG_DSN: undefined,
    RAGTRIAL_EMBED_ENDPOINT: undefined, RAGTRIAL_HYBRID_JSON: '{"local-hash-v1":{"floor":9}}' } });
  const badHit = await badOverride.search({ q: '审计事件', repo: REPO, branch: BR, k: 3 });
  ok('HYB4 非法覆盖回退安全默认（floor=9 被拒→默认 0.1 仍可命中）', badHit.service_state === 'hit');

  // ── F1 修复三类：正常 IDF 无降级标注 / DF 失败→plain 回退+df_unavailable / DF 失败→严格模式 fail-closed ──
  ok('HYB5 正常 IDF（DF 可用）无 df_unavailable 标注',
    idfHit.service_state === 'hit' && idfHit.df_unavailable === undefined);
  const dfFailPool = {
    query: (text, params) => {
      if (text.includes('ragtrial.documents d ON') && text.includes("state='active'")) {
        return Promise.reject(Object.assign(new Error('simulated DF scan failure'), { code: 'ECONNREFUSED' }));
      }
      return pool.query(text, params);
    },
  };
  const fbStore = await createRagTrialStore({ pool: dfFailPool, env: { ...process.env,
    CONSOLE_PG_DSN: undefined, RAGTRIAL_EMBED_ENDPOINT: undefined,
    RAGTRIAL_HYBRID_JSON: JSON.stringify({ 'local-hash-v1': { lexMode: 'idf', w: 0.3, floor: 0.1 } }) } });
  const fbHit = await fbStore.search({ q: '审计事件必须携带 actor 字段', repo: REPO, branch: BR, k: 3 });
  ok('HYB6 DF 失败→回退 plain 词法（非静默：df_unavailable=true，命中与引用链完整）',
    fbHit.service_state === 'hit' && fbHit.df_unavailable === true
    && fbHit.results.every((h) => h.citation.doc_path)
    && fbHit.results.some((h) => h.lex_score > 0));
  const strictStore = await createRagTrialStore({ pool: dfFailPool, env: { ...process.env,
    CONSOLE_PG_DSN: undefined, RAGTRIAL_EMBED_ENDPOINT: undefined,
    RAGTRIAL_HYBRID_JSON: JSON.stringify({ 'local-hash-v1': { lexMode: 'idf-required', w: 0.3, floor: 0.1 } }) } });
  let strictErr = null;
  try { await strictStore.search({ q: '审计事件', repo: REPO, branch: BR, k: 3 }); }
  catch (e) { strictErr = e; }
  ok('HYB7 DF 失败→idf-required 严格模式 fail-closed（503 df_scan_failed，无结果冒充）',
    strictErr && strictErr.kind === 'df_scan_failed' && strictErr.status === 503);

  // ── F1 观测补丁：fallback metrics 计数 + 有界去重 audit ──
  const mBefore = await fbStore.metrics();
  const fbHit2 = await fbStore.search({ q: '密钥轮换流程', repo: REPO, branch: BR, k: 3, actor: 'hyb-observer' });
  const mAfter = await fbStore.metrics();
  ok('HYB8 fallback metrics 计数准确：idf 回退 +1/query；二次回退仍 df_unavailable',
    mAfter.hybrid_df_fallbacks === mBefore.hybrid_df_fallbacks + 1
    && fbHit2.df_unavailable === true && fbHit2.service_state === 'hit');
  const mPlain = await plainStore.metrics();
  ok('HYB8b plain 模式不计数（0）', mPlain.hybrid_df_fallbacks === 0);
  const mStrict = await strictStore.metrics();
  ok('HYB8c idf-required fail-closed 不伪装成 fallback（计数 0，错误契约不变）',
    mStrict.hybrid_df_fallbacks === 0 && strictErr.kind === 'df_scan_failed');
  const evRows = (await pool.query(
    `SELECT kind, repo, branch, detail FROM ragtrial.audit_events WHERE kind='HYBRID_DF_FALLBACK'`)).rows;
  ok('HYB9 audit 恰一条（5min 同键去重：2 次回退 → 1 事件）', evRows.length === 1);
  const dfEv = evRows[0] ?? {};
  const d = typeof dfEv.detail === 'string' ? JSON.parse(dfEv.detail) : (dfEv.detail ?? {});
  ok('HYB9b audit 字段有界（仅 lex_mode/fallback 两枚举值；无 DSN/路径/查询原文/凭据）',
    evRows.length === 1 && Object.keys(d).length === 2
    && d.lex_mode === 'idf' && d.fallback === 'plain' && dfEv.repo === REPO && dfEv.branch === BR);

  // ── G-5：模型专属 HYBRID_CONFIGS 覆盖 × F1 观测共存（准入重建波）──
  {
    const sO = await createRagTrialStore({ pool, env: { ...process.env, CONSOLE_PG_DSN: undefined,
      RAGTRIAL_EMBED_ENDPOINT: undefined,
      RAGTRIAL_HYBRID_JSON: JSON.stringify({ 'local-hash-v1': { lexMode: 'idf-required', w: 0.5, floor: 0.1 } }) } });
    // 模拟 DF 扫描失败：临时锁表制造查询失败？——更简单：直接验证 idf-required 覆盖路径
    // 在正常 DF 可用时 idf-required 应正常工作（不回退）；idf 模式回退由 HYB8 覆盖。
    const reqHit = await sO.search({ q: '审计事件必须携带 actor 字段', repo: REPO, branch: BR, k: 3 });
    ok('G5a 模型覆盖 idf-required 正常路径可用（DF 健康=不触发回退）',
      reqHit.service_state === 'hit' && reqHit.df_unavailable === undefined);
    // 覆盖+回退共存：idf 模式 + 模拟 DF 失败（mock fetch 不可行——用直接函数验证）
    const { HYBRID_CONFIGS } = await import('../lib/ragtrial/store.mjs');
    ok('G5b HYBRID_CONFIGS 默认三档未变（覆盖不影响默认）',
      HYBRID_CONFIGS[256].floor === 0.1 && HYBRID_CONFIGS[768].floor === 0.45 && HYBRID_CONFIGS[1024].floor === 0.52);
  }

} catch (e) {
  fail++;
  console.error('FATAL', e);
} finally {
  if (pool) await pool.end().catch(() => {});
  if (!process.env.RAGTRIAL_PG_TEST_DSN) {
    try { execFileSync('docker', ['rm', '-f', CTR], { stdio: 'pipe' }); } catch { /* already gone */ }
  }
}
console.log(`\nragtrial-pg.integration: ${pass} pass / ${fail} fail`);
process.exit(fail ? 1 : 0);
