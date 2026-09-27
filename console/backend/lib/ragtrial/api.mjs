// ragtrial/api.mjs — /api/rag-trial/* HTTP 路由（LOCAL_RAG_TRIAL）。
//
// 与 A 链（/api/rag/org-search，lexical，reference-only）完全并行、互不影响；
// 与 C 链（cchain）互不接触。全部端点需会话（401 未登录）。
// PG 未接线（CONSOLE_PG_DSN 未配置）→ 如实 BACKEND_NOT_WIRED，不伪装。

import fs from 'node:fs';
import path from 'node:path';
import { createRagTrialStore, RagTrialError } from './store.mjs';
import {
  toAuxEvidence, canAutoPromote, fixerPatchInputs, verifierAccepts,
  promotionRequest, attachToRun,
} from './review.mjs';

const BODY_LIMIT = 8 * 1024 * 1024; // 语料摄取走 body（默认 64KB 不够）
const inMemory = { error_states: {} }; // PG 不可达时的 error 态计数（PG 日志不可能写下）

function errKindCount(kind) {
  inMemory.error_states[kind] = (inMemory.error_states[kind] ?? 0) + 1;
}
let storePromise = null;
async function getStore(env) {
  if (!env.CONSOLE_PG_DSN) return null;
  if (!storePromise) {
    storePromise = (async () => {
      const pg = await import('pg').catch(() => null);
      if (!pg) throw new RagTrialError('pg module unavailable', 'pg_unavailable', 503);
      const pool = new pg.Pool({ connectionString: env.CONSOLE_PG_DSN, max: 4 });
      // PG 中断时 idle 连接会发 'error' 事件；不挂监听会成为未捕获异常把进程打死
      // （S12 实测坑）。查询路径自己处理连接错误，这里只记录保活。
      pool.on('error', (err) => {
        errKindCount('pool_connection_error');
        console.error(`[ragtrial] pg pool error (kept alive): ${String(err.message).slice(0, 120)}`);
      });
      const store = await createRagTrialStore({ pool, env });
      await store.initSchema();
      return store;
    })().catch((e) => { storePromise = null; throw e; });
  }
  return storePromise;
}

async function buildS3(env) {
  const endpoint = env.RAGTRIAL_S3_ENDPOINT;
  if (!endpoint) return { configured: false };
  const { createArtifactStore } = await import('../fxv/artifacts.mjs');
  return createArtifactStore({
    endpoint,
    bucket: env.RAGTRIAL_S3_BUCKET || 'ragtrial',
    accessKey: env.RAGTRIAL_S3_ACCESS_KEY,
    secretKey: env.RAGTRIAL_S3_SECRET_KEY,
  });
}

function readCorpusDir(dir, { base }) {
  // 语料目录只允许 env 声明的根下（容器内 ro 挂载 /app/rag-corpus）
  const root = path.resolve(base);
  const abs = path.resolve(dir);
  if (abs !== root && !abs.startsWith(root + path.sep)) {
    throw new RagTrialError(`corpus dir 越界（仅允许 ${root} 下）`, 'corpus_dir_forbidden', 400);
  }
  const out = [];
  for (const name of fs.readdirSync(abs).sort()) {
    if (!name.endsWith('.md') && !name.endsWith('.txt')) continue;
    const p = path.join(abs, name);
    if (!fs.statSync(p).isFile()) continue;
    out.push({ path: name, text: fs.readFileSync(p, 'utf8') });
  }
  if (!out.length) throw new RagTrialError(`corpus dir 无 .md/.txt 文档: ${abs}`, 'corpus_empty', 400);
  return out;
}

export async function ragTrialApi(req, res, ctx) {
  const { p, sendJson, readJsonBody, requireSession } = ctx;
  const env = process.env;
  const actorOf = (auth) => auth?.user?.name || auth?.user || 'rag-trial-operator';

  // 全部端点需会话
  const auth = await requireSession();
  if (!auth) return sendJson(res, 401, { error: { reason: 'unauthorized' } });
  const actor = actorOf(auth);

  let store;
  try { store = await getStore(env); }
  catch (e) {
    const kind = e instanceof RagTrialError ? e.kind : 'pg_unavailable';
    errKindCount(kind);
    return sendJson(res, 503, {
      service_state: 'error', error_kind: kind,
      in_memory_error_states: inMemory.error_states,
      note: 'ragtrial 存储不可用——显式错误，不伪装',
    });
  }
  if (!store) {
    return sendJson(res, 200, {
      service_state: 'backend_not_wired',
      note: 'CONSOLE_PG_DSN 未配置 — ragtrial API 返回 BACKEND_NOT_WIRED（不伪装检索）',
    });
  }

  const json = async (limit = 64 * 1024) => readJsonBody(req, limit);

  try {
    // ── 状态 ──
    if (p === '/api/rag-trial/status' && req.method === 'GET') {
      return sendJson(res, 200, { service_state: 'ready', ...(await store.status()) });
    }

    // ── 摄取（body docs 或 corpus_dir） ──
    if (p === '/api/rag-trial/ingest' && req.method === 'POST') {
      const body = await json(BODY_LIMIT) ?? {};
      const repo = String(body.repo || '');
      const branch = String(body.branch || '');
      if (!repo || !branch) return sendJson(res, 400, { error: { reason: 'repo/branch required' } });
      let docs = body.docs;
      if (!docs && body.corpus_dir) {
        docs = readCorpusDir(body.corpus_dir, { base: env.RAGTRIAL_CORPUS_DIR || '/app/rag-corpus' });
      }
      if (!Array.isArray(docs) || !docs.length || docs.some((d) => !d?.path || typeof d.text !== 'string')) {
        return sendJson(res, 400, { error: { reason: 'docs[{path,text}] 或 corpus_dir 必填' } });
      }
      const s3 = await buildS3(env);
      if (s3.configured) await s3.ensureBucket(); // 幂等（已存在→409 容忍）
      const r = await store.ingestDocuments({ docs, repo, branch, actor, objectStore: s3.configured ? s3 : null });
      return sendJson(res, 200, r);
    }

    // ── 删除文档 ──
    if (p === '/api/rag-trial/delete' && req.method === 'POST') {
      const body = await json() ?? {};
      const { repo, branch, doc_path } = body;
      if (!repo || !branch || !doc_path) {
        return sendJson(res, 400, { error: { reason: 'repo/branch/doc_path required' } });
      }
      return sendJson(res, 200, await store.deleteDocument({ repo, branch, doc_path }, { actor }));
    }

    // ── 查询（六状态） ──
    if (p === '/api/rag-trial/query' && req.method === 'POST') {
      const body = await json() ?? {};
      const q = String(body.q || '');
      const repo = String(body.repo || '');
      const branch = String(body.branch || '');
      if (!q || !repo || !branch) {
        return sendJson(res, 400, { error: { reason: 'q/repo/branch required' } });
      }
      const r = await store.search({
        q, repo, branch,
        k: Number(body.k || 5),
        modelId: body.model_id ? String(body.model_id) : null,
        actor,
      });
      return sendJson(res, 200, r);
    }

    // ── 指标 ──
    if (p === '/api/rag-trial/metrics' && req.method === 'GET') {
      const m = await store.metrics();
      return sendJson(res, 200, {
        ...m,
        in_memory_error_states: inMemory.error_states,
        note: 'error 态（PG 不可达）无法写 PG 日志，由进程内存计数补齐',
      });
    }

    // ── QA 评测（Recall@K） ──
    if (p === '/api/rag-trial/eval' && req.method === 'POST') {
      const body = await json(BODY_LIMIT) ?? {};
      const { repo, branch } = body;
      const qa = body.qa;
      if (!repo || !branch || !Array.isArray(qa) || !qa.length
          || qa.some((i) => !i?.q || !i?.expect_doc)) {
        return sendJson(res, 400, { error: { reason: 'repo/branch/qa[{q,expect_doc}] required' } });
      }
      return sendJson(res, 200, await store.evalQa({
        qa, repo, branch, k: Number(body.k || 5),
        qaSet: String(body.qa_set || 'inline'), actor,
      }));
    }

    // ── 索引失效 / 回滚 ──
    if (p === '/api/rag-trial/index/invalidate' && req.method === 'POST') {
      const body = await json() ?? {};
      return sendJson(res, 200, await store.invalidateIndex({
        modelId: body.model_id ? String(body.model_id) : null, actor,
      }));
    }
    if (p === '/api/rag-trial/index/rollback' && req.method === 'POST') {
      const body = await json() ?? {};
      const toVersion = Number(body.to_index_version);
      if (!Number.isInteger(toVersion) || toVersion < 1) {
        return sendJson(res, 400, { error: { reason: 'to_index_version (int>=1) required' } });
      }
      return sendJson(res, 200, await store.rollbackIndex({
        toVersion, modelId: body.model_id ? String(body.model_id) : null, actor,
      }));
    }

    // ── Review 联动（辅助证据 + 策略演示） ──
    if (p === '/api/rag-trial/review-aux' && req.method === 'POST') {
      const body = await json() ?? {};
      const { q, repo, branch, run_id } = body;
      if (!q || !repo || !branch) {
        return sendJson(res, 400, { error: { reason: 'q/repo/branch required' } });
      }
      const r = await store.search({ q, repo, branch, k: Number(body.k || 5), actor });
      const aux = (r.results ?? []).map((h) => toAuxEvidence(h, { runId: run_id ?? null }));
      const runView = attachToRun(
        { run_id: run_id ?? 'ad-hoc', findings: [], tickets: [], gates: [] }, aux);
      return sendJson(res, 200, {
        service_state: r.service_state,
        run_view: runView,
        policy: canAutoPromote(aux),
        note: 'RAG 命中仅作 Review 辅助引用；findings/tickets/gates 未被触碰',
      });
    }
    if (p === '/api/rag-trial/policy-check' && req.method === 'POST') {
      const body = await json() ?? {};
      const evidence = body.evidence ?? [];
      return sendJson(res, 200, {
        auto_promote: canAutoPromote(evidence),
        as_finding: promotionRequest({ target: 'finding', evidence }),
        as_ticket: promotionRequest({ target: 'ticket', evidence }),
        as_gate: promotionRequest({ target: 'gate', evidence }),
        fixer: fixerPatchInputs(evidence),
        verifier: Array.isArray(evidence)
          ? evidence.map((e) => verifierAccepts(e))
          : [verifierAccepts(evidence)],
      });
    }

    return sendJson(res, 404, { error: { reason: `unknown rag-trial path: ${p}` } });
  } catch (e) {
    if (e instanceof RagTrialError) {
      if (e.kind === 'pg_unavailable' || e.kind === 'pgvector_unavailable') errKindCount(e.kind);
      return sendJson(res, e.status, {
        service_state: e.kind === 'provider_unavailable' ? 'provider_unavailable' : 'error',
        error_kind: e.kind,
        error: e.message,
        in_memory_error_states: inMemory.error_states,
        note: 'ragtrial 显式失败——不伪装为空结果',
      });
    }
    // 停库期间 DNS/连接类错误（如 EAI_AGAIN/ENOTFOUND）同样归类 pg_unavailable
    const pgCodes = ['ECONNREFUSED', 'ETIMEDOUT', 'EAI_AGAIN', 'ENOTFOUND', '28P01', '3D000'];
    if (pgCodes.includes(e?.code)) {
      errKindCount('pg_unavailable');
      return sendJson(res, 503, {
        service_state: 'error', error_kind: 'pg_unavailable',
        error: String(e.message).slice(0, 120),
        in_memory_error_states: inMemory.error_states,
        note: 'PG 不可达——显式错误，不伪装为空结果',
      });
    }
    const msg = String(e?.message || e).slice(0, 200);
    errKindCount('internal');
    return sendJson(res, 500, {
      service_state: 'error', error_kind: 'internal', error: msg,
      in_memory_error_states: inMemory.error_states,
    });
  }
}
