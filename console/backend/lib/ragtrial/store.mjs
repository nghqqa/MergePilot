// ragtrial/store.mjs — RAG trial PG 存储 + 检索状态机（LOCAL_RAG_TRIAL）。
//
// 查询六状态（service_state，全部如实返回，绝不伪装空成功）：
//   hit / empty / model_missing / index_stale / provider_unavailable / error
// 附加维度：error_kind ∈ {pg_unavailable, pgvector_unavailable, internal}。
//
// 关键不变量：
//  1) 任何检索命中必须携带完整引用（repo/branch/doc_path/line_start/line_end/
//     doc_sha256/chunk_sha256/model_digest/index_version）；引用缺失行被丢弃并
//     计数 dropped_uncited + 审计，绝不返回无引用结果。
//  2) chunk 行的 model_digest/index_version 与 active model 不一致时：
//     全部不一致 → index_stale；部分不一致 → 命中可返回但报告 drifted_rows。
//  3) 删除 = chunks 物理删除 + documents.state='deleted'（保留审计行），
//     删除后该文档任何词都不可再检索（验证由测试保证）。
//  4) audit_events.actor NOT NULL（沿用 fxv 契约）。

import { RAGTRIAL_SCHEMA_SQL, RETAINED_INDEX_VERSIONS, chunkTableFor } from './schema.mjs';
import {
  LOCAL_MODEL_ID, localModelSpec, modelDigest, resolveProvider,
  embedBatch, embedLocal, tokenize, vectorLiteral,
  ProviderUnavailableError, ModelBlockedError, sha256hex,
} from './embed.mjs';
import { prepareDocument } from './ingest.mjs';

export const QUERY_STATES = [
  'hit', 'empty', 'model_missing', 'index_stale', 'provider_unavailable', 'error',
];

// 混合打分配置（按模型维度区分；校准证据见 evidence/rag-zh-gate/*/calibration.json
// 与 evidence/rag-prod/*/benchmark.json）：
//   final = w·vec + (1-w)·lex，lex 按 lexMode ∈ {plain, idf, idf-required}：
//   plain = |q∩d|/min(|q|,|d|)；idf = Σ idf(q∩d)/Σ idf(q)（语料驱动 DF，缓存 5min；
//   DF 扫描失败→回退 plain + df_unavailable=true，不缓存失败）；idf-required = 严格
//   IDF 契约（DF 失败→fail-closed 503 df_scan_failed，绝不静默退化）。
// 默认值 = 各模型部署现行值（本波零行为变化）。校准结论：现有模型在空准确=1.0
// 约束下同义/跨表述上限不足（能力界，非调参界）——生产 zh 模型到位后经
// calibrate-hybrid.mjs + holdout 重校准并通过 RAGTRIAL_HYBRID_JSON 覆盖验证。
export const HYBRID_CONFIGS = {
  256: { w: 0.5, lexMode: 'plain', floor: 0.1 },
  768: { w: 0.5, lexMode: 'plain', floor: 0.45 },
  1024: { w: 0.5, lexMode: 'plain', floor: 0.52 },
};

export class RagTrialError extends Error {
  constructor(message, kind = 'internal', status = 500) {
    super(message); this.name = 'RagTrialError'; this.kind = kind; this.status = status;
  }
}

// 内部试验用覆盖：RAGTRIAL_HYBRID_JSON={"<model_id>":{"w":..,"lexMode":..,"floor":..}}
// （模型 id 优先，dims 默认兜底；不影响生产默认值——e5 内部试验高召回档等用途）
function hybridConfigFor(env, model) {
  let override = null;
  if (env.RAGTRIAL_HYBRID_JSON) {
    try {
      const o = JSON.parse(env.RAGTRIAL_HYBRID_JSON);
      override = o?.[model.model_id] ?? o?.[`dims:${model.dims}`] ?? null;
    } catch { override = null; /* 非法 JSON 静默忽略，走安全默认 */ }
  }
  const base = HYBRID_CONFIGS[Number(model.dims)] ?? { w: 0.5, lexMode: 'plain', floor: 0.1 };
  const cfg = { ...base, ...(override ?? {}) };
  if (!(cfg.w >= 0 && cfg.w <= 1) || !['plain', 'idf', 'idf-required'].includes(cfg.lexMode)
      || !(cfg.floor >= 0 && cfg.floor <= 1)) return base;
  return cfg;
}

export async function createRagTrialStore({ pool, env = process.env, fetchImpl = fetch }) {
  if (!pool || typeof pool.query !== 'function') {
    throw new Error('createRagTrialStore: pool with .query() required (pg Pool)');
  }
  const q = (text, params) => pool.query(text, params);
  let pgvectorOk = null; // 三态：null=未知 true=可用 false=缺失
  const dfCache = new Map(); // scope|digest|version → {at, N, df:Map}（5min TTL）

  async function audit(kind, actor, { repo = null, branch = null, detail = {} } = {}) {
    if (!actor) throw new Error('audit: actor required (NOT NULL)');
    await q(
      `INSERT INTO ragtrial.audit_events (kind, actor, repo, branch, detail)
       VALUES ($1,$2,$3,$4,$5::jsonb)`,
      [kind, actor, repo, branch, JSON.stringify(detail)]);
  }

  async function initSchema() {
    try {
      await q(`CREATE EXTENSION IF NOT EXISTS vector`);
      pgvectorOk = true;
    } catch (e) {
      pgvectorOk = false;
      throw new RagTrialError(
        `pgvector extension unavailable: ${String(e.message).slice(0, 120)}`,
        'pgvector_unavailable', 503);
    }
    for (const sql of RAGTRIAL_SCHEMA_SQL) await q(sql);
    // 默认注册本地模型（幂等）
    return registerModel(localModelSpec(), 'local', { actor: 'system', silent: true });
  }

  async function ensurePgvector() {
    if (pgvectorOk === null) {
      try {
        const r = await q(`SELECT extname FROM pg_extension WHERE extname='vector'`);
        pgvectorOk = r.rows.length > 0;
      } catch { pgvectorOk = false; }
    }
    if (!pgvectorOk) {
      throw new RagTrialError('pgvector unavailable in this database', 'pgvector_unavailable', 503);
    }
  }

  // ── 模型注册/解析 ─────────────────────────────────────────────
  async function registerModel(spec, providerKind = 'local', { actor = 'rag-trial-operator', force = false, silent = false, dims = null, manifest = null } = {}) {
    await ensurePgvector();
    const digest = modelDigest(spec);
    const modelDims = Number(dims ?? spec?.dims ?? 256);
    const existing = await q(`SELECT * FROM ragtrial.models WHERE model_id=$1`, [spec.model_id]);
    if (existing.rows.length) {
      const row = existing.rows[0];
      if (row.model_digest !== digest && !force) {
        throw new RagTrialError(
          `model_digest conflict for ${spec.model_id}: indexed=${row.model_digest} new=${digest}（force=true 方可覆盖）`,
          'model_digest_conflict', 409);
      }
      if (row.model_digest === digest) {
        if (!silent) await audit('MODEL_REGISTER', actor, { detail: { model_id: spec.model_id, model_digest: digest, idempotent: true } });
        return { model_id: spec.model_id, model_digest: digest, index_version: row.index_version, dims: row.dims, idempotent: true };
      }
      // force 覆盖 → index_version 递增（旧版本行保留供回滚）
      const r = await q(
        `UPDATE ragtrial.models SET model_digest=$2, spec=$3::jsonb, provider_kind=$4,
           index_version=index_version+1, dims=$5, manifest=$6::jsonb, updated_at=now()
         WHERE model_id=$1 RETURNING *`,
        [spec.model_id, digest, JSON.stringify(spec), providerKind, modelDims, manifest ? JSON.stringify(manifest) : null]);
      await audit('MODEL_REGISTER', actor, { detail: { model_id: spec.model_id, model_digest: digest, index_version: r.rows[0].index_version, dims: modelDims, force } });
      return { model_id: spec.model_id, model_digest: digest, index_version: r.rows[0].index_version, dims: modelDims, force };
    }
    await q(
      `INSERT INTO ragtrial.models (model_id, model_digest, spec, provider_kind, dims, manifest)
       VALUES ($1,$2,$3::jsonb,$4,$5,$6::jsonb)`,
      [spec.model_id, digest, JSON.stringify(spec), providerKind, modelDims, manifest ? JSON.stringify(manifest) : null]);
    if (!silent) await audit('MODEL_REGISTER', actor, { detail: { model_id: spec.model_id, model_digest: digest, dims: modelDims } });
    return { model_id: spec.model_id, model_digest: digest, index_version: 1, dims: modelDims };
  }

  async function resolveModel(modelId) {
    if (modelId) {
      const r = await q(`SELECT * FROM ragtrial.models WHERE model_id=$1 AND active`, [modelId]);
      return r.rows[0] ?? null;
    }
    const r = await q(`SELECT * FROM ragtrial.models WHERE active ORDER BY created_at LIMIT 1`);
    return r.rows[0] ?? null;
  }

  // ── 摄取（增量）/ 删除 ────────────────────────────────────────
  // modelId 指定目标模型（local-hash-v1 或语义模型）；按 models.dims 路由到
  // 白名单物理表（256→chunks / 1024→chunks_semantic），其它维度 fail-closed。
  async function ingestDocuments({ docs, repo, branch, actor = 'rag-trial-operator', objectStore = null, modelId = null }) {
    await ensurePgvector();
    const model = await resolveModel(modelId);
    if (!model) throw new RagTrialError(`no active model registered: ${modelId ?? '(default)'}`, 'model_missing', 409);
    const table = chunkTableFor(model.dims);
    // provider 按模型选择（models.provider_kind）：local 永远本地确定性嵌入，
    // remote 走 env 配置的 attested sidecar——env 有 sidecar 不影响 local 模型。
    const embedFn = model.provider_kind === 'remote'
      ? (texts) => embedBatch(resolveProvider(env), texts, { fetchImpl, mode: 'passage' })
      : (texts) => texts.map((t) => embedLocal(t));
    const report = [];
    const client = await pool.connect();
    try {
      await client.query('BEGIN');
      for (const d of docs) {
        const doc = await prepareDocument({ repo, branch, doc_path: d.path, text: d.text }, embedFn);
        // 维度 fail-closed：provider 输出必须与 models.dims 一致（pgvector 列宽也会拦，这里前置给出清晰错误）
        for (const c of doc.chunks) {
          if (c.embedding && c.embedding.length !== Number(model.dims)) {
            throw new RagTrialError(
              `embedding 维度不匹配：model ${model.model_id} dims=${model.dims}，chunk 输出 ${c.embedding.length}（fail-closed）`,
              'dimension_mismatch', 422);
          }
        }
        // MinIO 内容寻址原始文档归档（可选；失败不阻断摄取，如实记录）
        let objectKey = null, objectStatus = 'not_configured';
        if (objectStore && objectStore.configured) {
          try {
            const r = await objectStore.putContent(
              `ragtrial/docs/${repo}/${branch}/${encodeURIComponent(d.path)}`, d.text);
            if (r.ok) { objectKey = r.key; objectStatus = 'verified_readback'; }
            else objectStatus = r.reason;
          } catch (e) { objectStatus = `error:${String(e.message).slice(0, 60)}`; }
        }
        const cur = await client.query(
          `SELECT * FROM ragtrial.documents WHERE repo=$1 AND branch=$2 AND doc_path=$3`,
          [repo, branch, d.path]);
        const row = cur.rows[0];
        if (row && row.state === 'active' && row.doc_sha256 === doc.doc_sha256
            && row.index_version === model.index_version && row.model_id === model.model_id) {
          report.push({ doc_path: d.path, action: 'unchanged', chunks: row.chunk_count });
          continue;
        }
        // 变更/新文档：替换当前版本 chunk 行（旧版本行保留）
        await client.query(
          `DELETE FROM ${table}
            WHERE repo=$1 AND branch=$2 AND doc_path=$3 AND index_version=$4`,
          [repo, branch, d.path, model.index_version]);
        for (const c of doc.chunks) {
          await client.query(
            `INSERT INTO ${table}
               (repo,branch,doc_path,doc_sha256,chunk_index,chunk_sha256,para_index,
                line_start,line_end,char_start,char_end,text,embedding,
                model_id,model_digest,index_version)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::vector,$14,$15,$16)`,
            [repo, branch, d.path, doc.doc_sha256, c.chunk_index, c.chunk_sha256, c.para_index,
             c.line_start, c.line_end, c.char_start, c.char_end, c.text,
             vectorLiteral(c.embedding), model.model_id, model.model_digest, model.index_version]);
        }
        await client.query(
          `INSERT INTO ragtrial.documents
             (repo,branch,doc_path,doc_sha256,content_bytes,chunk_count,state,
              model_id,model_digest,index_version,object_key,updated_at)
           VALUES ($1,$2,$3,$4,$5,$6,'active',$7,$8,$9,$10,now())
           ON CONFLICT (repo,branch,doc_path) DO UPDATE SET
             doc_sha256=EXCLUDED.doc_sha256, content_bytes=EXCLUDED.content_bytes,
             chunk_count=EXCLUDED.chunk_count, state='active',
             model_id=EXCLUDED.model_id, model_digest=EXCLUDED.model_digest,
             index_version=EXCLUDED.index_version, object_key=EXCLUDED.object_key,
             updated_at=now()`,
          [repo, branch, d.path, doc.doc_sha256, doc.content_bytes, doc.chunks.length,
           model.model_id, model.model_digest, model.index_version, objectKey]);
        report.push({
          doc_path: d.path,
          action: row ? 'updated' : 'ingested',
          chunks: doc.chunks.length,
          duplicates_dropped: doc.stats.duplicates,
          doc_sha256: doc.doc_sha256,
          object_status: objectStatus,
        });
      }
      await client.query('COMMIT');
    } catch (e) {
      await client.query('ROLLBACK').catch(() => {});
      client.release();
      throw wrapEmbedError(e);
    }
    client.release();
    await pruneOldVersions(repo, branch, table);
    const counts = report.reduce((acc, r) => { acc[r.action] = (acc[r.action] ?? 0) + 1; return acc; }, {});
    await audit('INGEST', actor, {
      repo, branch,
      detail: { docs: report.length, ...counts, model_id: model.model_id, model_digest: model.model_digest, index_version: model.index_version },
    });
    return { ok: true, model: { model_id: model.model_id, model_digest: model.model_digest, index_version: model.index_version, dims: Number(model.dims) }, report };
  }

  function wrapEmbedError(e) {
    if (e instanceof RagTrialError) return e;
    if (e instanceof ModelBlockedError) {
      return new RagTrialError(e.message, e.reason ?? 'model_attestation_failed', 503);
    }
    if (e instanceof ProviderUnavailableError) {
      return new RagTrialError(e.message, 'provider_unavailable', 503);
    }
    if (e?.kind === 'dimension_mismatch') {
      return new RagTrialError(e.message, 'dimension_mismatch', 422);
    }
    return e;
  }

  async function pruneOldVersions(repo, branch, table = 'ragtrial.chunks') {
    // 只保留最近 RETAINED_INDEX_VERSIONS 个版本的 chunk 行（回滚窗口）
    await q(
      `DELETE FROM ${table}
        WHERE repo=$1 AND branch=$2 AND index_version NOT IN (
          SELECT index_version FROM ${table}
           WHERE repo=$1 AND branch=$2
           GROUP BY index_version ORDER BY index_version DESC LIMIT $3)`,
      [repo, branch, RETAINED_INDEX_VERSIONS]);
  }

  async function deleteDocument({ repo, branch, doc_path }, { actor = 'rag-trial-operator' } = {}) {
    await ensurePgvector();
    let removed = 0;
    for (const table of ['ragtrial.chunks', 'ragtrial.chunks_semantic', 'ragtrial.chunks_semantic_768']) {
      const r = await q(`DELETE FROM ${table}
          WHERE repo=$1 AND branch=$2 AND doc_path=$3 RETURNING chunk_id`,
        [repo, branch, doc_path]);
      removed += r.rowCount;
    }
    await q(
      `UPDATE ragtrial.documents SET state='deleted', chunk_count=0, updated_at=now()
        WHERE repo=$1 AND branch=$2 AND doc_path=$3`,
      [repo, branch, doc_path]);
    await audit('DOC_DELETE', actor, { repo, branch, detail: { doc_path, chunks_removed: removed } });
    return { ok: true, doc_path, chunks_removed: removed };
  }

  // ── 检索（六状态） ────────────────────────────────────────────
  async function search({ q: queryText, repo, branch, k = 5, actor = 'rag-trial-operator', modelId = null }) {
    const started = Date.now();
    const finish = async (state, extra = {}, log = true) => {
      const latency = Date.now() - started;
      if (log) {
        await q(
          `INSERT INTO ragtrial.query_log
             (actor,state,repo,branch,model_id,model_digest,k,latency_ms,hits,cited_hits,dropped_uncited,query_text,detail)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb)`,
          [actor, state, repo, branch, extra.model_id ?? null, extra.model_digest ?? null,
           k, latency, extra.hits ?? 0, extra.cited_hits ?? 0, extra.dropped_uncited ?? 0,
           String(queryText ?? '').slice(0, 200),
           JSON.stringify(extra.detail ?? {})]).catch(() => {});
      }
      return { service_state: state, latency_ms: latency, ...extra };
    };

    // 1) 模型解析（model_missing）
    let model;
    try { model = await resolveModel(modelId); }
    catch (e) { throw pgWrap(e); }
    if (!model) {
      const r = await finish('model_missing', { model_id: modelId ?? null, results: [],
        note: `模型未注册：${modelId ?? '(default)'}` });
      await audit('QUERY_MODEL_MISSING', actor, { repo, branch, detail: { model_id: modelId ?? null } });
      return r;
    }

    // 2) 范围检查（空索引 / stale）—— 按 models.dims 路由物理表
    let table;
    try { table = chunkTableFor(model.dims); }
    catch (e) { throw wrapEmbedError(e); }
    let scope;
    try {
      scope = await q(
        `SELECT count(*)::int total,
                count(*) FILTER (WHERE model_digest=$3 AND index_version=$4)::int live,
                count(*) FILTER (WHERE model_digest<>$3 OR index_version<>$4)::int drifted,
                count(*) FILTER (WHERE line_start IS NULL OR line_end IS NULL OR doc_path IS NULL)::int uncited
           FROM ${table}
          WHERE repo=$1 AND branch=$2`,
        [repo, branch, model.model_digest, model.index_version]);
    } catch (e) { throw pgWrap(e); }
    const { total, live, drifted, uncited } = scope.rows[0];
    if (total === 0) {
      return finish('empty', { model_id: model.model_id, model_digest: model.model_digest,
        results: [], index_state: 'none',
        note: '该 repo/branch 无任何索引数据' });
    }
    if (live === 0) {
      const r = await finish('index_stale', {
        model_id: model.model_id, model_digest: model.model_digest,
        results: [], index_state: 'stale',
        detail: { total_rows: total, drifted_rows: drifted },
        note: '索引行全部与 active model 的 digest/index_version 不一致（digest 漂移或版本失效）',
      });
      await audit('QUERY_INDEX_STALE', actor, { repo, branch, detail: { total_rows: total, drifted_rows: drifted } });
      return r;
    }

    // 3) 查询向量化（按模型 provider_kind；provider_unavailable=网络不可达；ModelBlocked=fail-closed）
    let qvec;
    const provider = model.provider_kind === 'remote' ? resolveProvider(env) : { kind: 'local' };
    if (provider.kind === 'remote') {
      try { qvec = (await embedBatch(provider, [queryText], { fetchImpl, mode: 'query' }))[0]; }
      catch (e) {
        if (e instanceof ProviderUnavailableError) {
          const r = await finish('provider_unavailable', {
            model_id: model.model_id, results: [], degraded_reason: 'embed_provider_unreachable',
            note: '嵌入 provider 不可达——显式降级，不伪装为空结果' });
          await audit('QUERY_PROVIDER_UNAVAILABLE', actor, { repo, branch, detail: { reason: 'embed_provider_unreachable' } });
          return r;
        }
        throw wrapEmbedError(e);
      }
    } else {
      qvec = embedLocal(queryText);
    }

    // 4) 向量检索 + 混合打分重排。
    //    候选池：pgvector cosine 取 k*3（≤30）行；JS 侧对每行计算
    //    final = 0.5·vec + 0.5·lex（lex = 查询/文本 token 精确交集 ÷ min(双方 token 数)）。
    //    词法精确交集无哈希碰撞噪声：零重叠查询 final ≤0.5·噪声(≈0.03) 稳落在地板下；
    //    真命中（多 token 重叠）final ≥0.15；单 token 精确命中 lex→1 也可靠命中。
    let rows;
    try {
      const pool2 = Math.min(Math.max(k * 3, 15), 30);
      const r = await q(
        `SELECT c.chunk_id, c.doc_path, c.doc_sha256, c.chunk_index, c.chunk_sha256,
                c.para_index, c.line_start, c.line_end, c.text,
                c.model_id, c.model_digest, c.index_version,
                1 - (c.embedding <=> $1::vector) AS vec_score
           FROM ${table} c
           JOIN ragtrial.documents d
             ON d.repo=c.repo AND d.branch=c.branch AND d.doc_path=c.doc_path AND d.state='active'
          WHERE c.repo=$2 AND c.branch=$3
            AND c.model_digest=$4 AND c.index_version=$5
            AND c.doc_path IS NOT NULL AND c.line_start IS NOT NULL AND c.line_end IS NOT NULL
          ORDER BY c.embedding <=> $1::vector
          LIMIT $6`,
        [vectorLiteral(qvec), repo, branch, model.model_digest, model.index_version, pool2]);
      rows = r.rows;
    } catch (e) { throw pgWrap(e); }

    const hyb = hybridConfigFor(env, model);
    const scoreFloor = hyb.floor;
    // IDF 词法：DF 取该 scope 全部活跃 chunk（真语料驱动；缓存 5min。生产大语料
    // 需持久化 DF 表——接口已按此设计预留，见报告）。
    // F1 修复（DF 失败不得静默退化）：DF 扫描失败时——
    //   * lexMode='idf'（默认语义）：回退 plain 词法并在响应标注 df_unavailable=true（透明降级）；
    //     失败结果不进 dfCache（下次查询重试 DF）。
    //   * lexMode='idf-required'（严格语义）：fail-closed 抛错（503 df_scan_failed），
    //     绝不以静默归零的 idf 或未经声明的 plain 冒充 idf 结果。
    let idfFn = null;
    let dfFallback = false;
    if (hyb.lexMode === 'idf' || hyb.lexMode === 'idf-required') {
      const strict = hyb.lexMode === 'idf-required';
      const key = `${repo}|${branch}|${model.model_digest}|${model.index_version}`;
      let entry = dfCache.get(key);
      if (!entry || Date.now() - entry.at > 300_000) {
        let r2 = null;
        try {
          r2 = await q(
            `SELECT c.text FROM ${table} c
               JOIN ragtrial.documents d ON d.repo=c.repo AND d.branch=c.branch AND d.doc_path=c.doc_path AND d.state='active'
              WHERE c.repo=$1 AND c.branch=$2`, [repo, branch]);
        } catch { r2 = null; }
        if (r2 === null) {
          if (strict) {
            throw new RagTrialError(
              'hybrid lexMode=idf-required: DF scan failed — fail-closed (no silent degradation)',
              'df_scan_failed', 503);
          }
          dfFallback = true;
        } else {
          const df = new Map();
          for (const row of r2.rows) for (const t of new Set(tokenize(row.text))) df.set(t, (df.get(t) ?? 0) + 1);
          entry = { at: Date.now(), N: r2.rows.length, df };
          dfCache.set(key, entry);
        }
      }
      if (!dfFallback && entry) idfFn = (t) => Math.log(1 + entry.N / (1 + (entry.df.get(t) ?? 0)));
    }
    const qTokens = [...new Set(tokenize(queryText))];
    const qIdfSum = idfFn ? qTokens.reduce((s2, t) => s2 + idfFn(t), 0) || 1 : 0;
    const scored = rows.map((r) => {
      const dTokens = new Set(tokenize(r.text));
      let shared = 0, sharedIdf = 0;
      for (const t of qTokens) if (dTokens.has(t)) { shared++; if (idfFn) sharedIdf += idfFn(t); }
      const lex = idfFn
        ? sharedIdf / qIdfSum
        : (qTokens.length && dTokens.size ? shared / Math.min(qTokens.length, dTokens.size) : 0);
      const vec = Number(r.vec_score);
      return { r, lex, vec, final: hyb.w * vec + (1 - hyb.w) * lex };
    })
      .filter((s) => s.final >= scoreFloor)
      .sort((a, b) => b.final - a.final)
      .slice(0, Math.min(Math.max(k, 1), 20));

    const results = scored.map(({ r, lex, vec, final }) => ({
      score: +final.toFixed(6),
      vec_score: +vec.toFixed(6),
      lex_score: +lex.toFixed(6),
      citation: {
        repo, branch,
        doc_path: r.doc_path,
        line_start: r.line_start,
        line_end: r.line_end,
        para_index: r.para_index,
        chunk_index: r.chunk_index,
        doc_sha256: r.doc_sha256,
        chunk_sha256: r.chunk_sha256,
        model_id: r.model_id,
        model_digest: r.model_digest,
        index_version: r.index_version,
      },
      snippet: r.text.length > 480 ? r.text.slice(0, 480) + '…' : r.text,
    }));

    const state = results.length ? 'hit' : 'empty';
    const extra = {
      model_id: model.model_id,
      model_digest: model.model_digest,
      index_version: model.index_version,
      results,
      score_floor: scoreFloor,
      hits: results.length,
      cited_hits: results.length, // 引用契约：返回的命中必带引用（缺失行已被过滤）
      dropped_uncited: uncited,
    };
    if (drifted > 0) extra.drifted_rows = drifted;
    if (dfFallback) {
      // F1 透明降级标注：机器字段（布尔）+ 中文说明，双通道可观测
      extra.df_unavailable = true;
      extra.note = 'DF 扫描失败——本次已回退 plain 词法（非静默降级）；lex 分数为 plain 口径';
    }
    const out = await finish(state, extra);
    if (uncited > 0) {
      await audit('CITATION_DROPPED', actor, { repo, branch, detail: { dropped: uncited } });
    }
    return out;
  }

  function pgWrap(e) {
    if (e instanceof RagTrialError) return e;
    const msg = String(e.message || e);
    const code = e.code ?? e.errno ?? e.cause?.code ?? null;
    const connKinds = ['ECONNREFUSED', 'ETIMEDOUT', 'EAI_AGAIN', 'ENOTFOUND', '28P01', '3D000'];
    const looksConn = connKinds.includes(code)
      || connKinds.some((k) => msg.includes(k))
      || msg.includes('Connection') || msg.includes('connection');
    if (looksConn) {
      return new RagTrialError(`PG unavailable: ${msg.slice(0, 120)}`, 'pg_unavailable', 503);
    }
    return new RagTrialError(msg.slice(0, 200), 'internal', 500);
  }

  // ── 索引版本操作（失效 / 回滚） ──────────────────────────────
  async function invalidateIndex({ modelId = null, actor = 'rag-trial-operator' } = {}) {
    const model = await resolveModel(modelId);
    if (!model) throw new RagTrialError('model not found', 'model_missing', 409);
    const r = await q(
      `UPDATE ragtrial.models SET index_version=index_version+1, updated_at=now()
        WHERE model_id=$1 RETURNING index_version`, [model.model_id]);
    const nv = r.rows[0].index_version;
    await audit('INDEX_INVALIDATE', actor, {
      detail: { model_id: model.model_id, from: model.index_version, to: nv,
        note: '旧版本行保留（回滚窗口），查询只命中新版本——re-ingest 前查询将 index_stale' },
    });
    return { ok: true, model_id: model.model_id, index_version: nv };
  }

  async function rollbackIndex({ toVersion, modelId = null, actor = 'rag-trial-operator' } = {}) {
    const model = await resolveModel(modelId);
    if (!model) throw new RagTrialError('model not found', 'model_missing', 409);
    const table = chunkTableFor(model.dims);
    const exists = await q(
      `SELECT 1 FROM ${table} WHERE index_version=$1 AND model_digest=$2 LIMIT 1`,
      [toVersion, model.model_digest]);
    if (!exists.rows.length) {
      throw new RagTrialError(
        `rollback target index_version=${toVersion} 无保留行（窗口=${RETAINED_INDEX_VERSIONS} 或已 pruned）`,
        'rollback_target_missing', 409);
    }
    await q(`UPDATE ragtrial.models SET index_version=$2, updated_at=now() WHERE model_id=$1`,
      [model.model_id, toVersion]);
    await audit('INDEX_ROLLBACK', actor, {
      detail: { model_id: model.model_id, from: model.index_version, to: toVersion },
    });
    return { ok: true, model_id: model.model_id, index_version: toVersion };
  }

  // ── QA 评测（Recall@K；modelId 可指定 provider） ──────────────
  async function evalQa({ qa, repo, branch, k = 5, qaSet = 'inline', actor = 'rag-trial-operator', modelId = null }) {
    const model = await resolveModel(modelId);
    if (!model) throw new RagTrialError('no active model registered', 'model_missing', 409);
    let hitAtK = 0;
    const detail = [];
    for (const item of qa) {
      const r = await search({ q: item.q, repo, branch, k, actor: 'eval-runner', modelId: model.model_id });
      const got = (r.results ?? []).map((x) => x.citation.doc_path);
      const hit = got.includes(item.expect_doc);
      if (hit) hitAtK++;
      detail.push({ q: item.q, expect_doc: item.expect_doc, got, hit, state: r.service_state });
    }
    const total = qa.length;
    const recall = total ? +(hitAtK / total).toFixed(4) : 0;
    const evalId = `eval-${Date.now().toString(36)}-${sha256hex(JSON.stringify(qa)).slice(0, 8)}`;
    await q(
      `INSERT INTO ragtrial.eval_runs (eval_id, qa_set, model_id, model_digest, k, total, hit_at_k, recall_at_k, detail)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)`,
      [evalId, qaSet, model.model_id, model.model_digest,
       k, total, hitAtK, recall, JSON.stringify(detail)]);
    await audit('EVAL_RUN', actor, { repo, branch, detail: { eval_id: evalId, recall_at_k: recall, k, total } });
    return { eval_id: evalId, k, total, hit_at_k: hitAtK, recall_at_k: recall, detail };
  }

  // ── 状态与指标（从真实表推导，无 mock） ──────────────────────
  async function status() {
    const models = await q(`SELECT model_id, model_digest, provider_kind, dims, index_version, active, updated_at FROM ragtrial.models ORDER BY created_at`);
    const docs = await q(
      `SELECT count(*)::int total,
              count(*) FILTER (WHERE state='active')::int active,
              count(*) FILTER (WHERE state='deleted')::int deleted
         FROM ragtrial.documents`);
    const chunks = await q(
      `SELECT (SELECT count(*)::int FROM ragtrial.chunks) hash_chunks,
              (SELECT count(*)::int FROM ragtrial.chunks_semantic) semantic_chunks,
              (SELECT count(*)::int FROM ragtrial.chunks_semantic_768) semantic768_chunks,
              (SELECT count(DISTINCT index_version)::int FROM ragtrial.chunks) hash_versions,
              (SELECT count(DISTINCT index_version)::int FROM ragtrial.chunks_semantic) semantic_versions`);
    const ext = await q(`SELECT extname, extversion FROM pg_extension WHERE extname='vector'`);
    return {
      pgvector: ext.rows[0] ?? null,
      models: models.rows,
      documents: docs.rows[0],
      chunks: {
        total: Number(chunks.rows[0].hash_chunks) + Number(chunks.rows[0].semantic_chunks) + Number(chunks.rows[0].semantic768_chunks ?? 0),
        hash: Number(chunks.rows[0].hash_chunks),
        semantic: Number(chunks.rows[0].semantic_chunks),
        semantic_768: Number(chunks.rows[0].semantic768_chunks ?? 0),
        versions: Number(chunks.rows[0].hash_versions) + Number(chunks.rows[0].semantic_versions),
      },
    };
  }

  async function metrics() {
    const byState = await q(
      `SELECT state, count(*)::int n FROM ragtrial.query_log GROUP BY state`);
    const latency = await q(
      `SELECT coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms),0)::int p50,
              coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms),0)::int p95
         FROM ragtrial.query_log`);
    const hits = await q(
      `SELECT count(*) FILTER (WHERE state='hit')::int queries_with_hits,
              coalesce(sum(hits),0)::int hit_rows,
              coalesce(sum(cited_hits),0)::int cited_rows,
              coalesce(sum(dropped_uncited),0)::int dropped_uncited_rows
         FROM ragtrial.query_log`);
    const evals = await q(
      `SELECT eval_id, qa_set, k, total, hit_at_k, recall_at_k, created_at
         FROM ragtrial.eval_runs ORDER BY created_at DESC LIMIT 5`);
    const auditKinds = await q(
      `SELECT kind, count(*)::int n FROM ragtrial.audit_events GROUP BY kind`);
    const h = hits.rows[0];
    return {
      queries_by_state: Object.fromEntries(byState.rows.map((r) => [r.state, r.n])),
      latency_ms: latency.rows[0],
      citation: {
        hit_rows: h.hit_rows, cited_rows: h.cited_rows, dropped_uncited_rows: h.dropped_uncited_rows,
        // cited_rows==hit_rows 时引用命中率 1.0（无引用命中会被丢弃，不可能出现在 hit 里）
      },
      eval_runs: evals.rows,
      audit_events_by_kind: Object.fromEntries(auditKinds.rows.map((r) => [r.kind, r.n])),
    };
  }

  return {
    initSchema, registerModel, resolveModel,
    ingestDocuments, deleteDocument, search,
    invalidateIndex, rollbackIndex, evalQa,
    status, metrics, audit, ensurePgvector,
  };
}
