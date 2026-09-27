// ragtrial/schema.mjs — RAG trial PG schema（幂等初始化；PRODUCTION_RAG_READINESS 扩展）。
//
// 命名空间 ragtrial 与 fxv 完全隔离；本栈 PG 为独立实例（local-rag-trial 栈），
// 绝不挂接 promote/staging/生产卷。
//
// 绑定模型：chunks 每行携带 repo/branch/doc_path/doc_sha256/chunk 指纹/
// model_id/model_digest/index_version —— 检索结果因此可给出完整引用链。
// 版本保留：重灌新 index_version 时保留旧版本行（最近 2 个版本），
// 供 index rollback 演练；查询只命中 active model 的 digest+version。
//
// PRODUCTION_RAG_READINESS 扩展（幂等 ALTER，兼容既有部署）：
//   * models.dims/manifest —— 模型维度与工件清单（sha256 pin，fail-closed 比对）；
//   * chunks_semantic —— vector(1024) 语义索引表（bge-large-en-v1.5）；
//     路由按 models.dims 白名单 {256,1024}，其它维度一律 dimension_mismatch；
//   * jobs —— 持久任务队列（幂等 dedupe_key/重试/超时心跳/死信），
//     生命周期审计走 ragtrial.audit_events（JOB_* 前缀）。

export const RAGTRIAL_SCHEMA_SQL = [
  `CREATE SCHEMA IF NOT EXISTS ragtrial`,
  `CREATE TABLE IF NOT EXISTS ragtrial.models (
     model_id      TEXT PRIMARY KEY,
     model_digest  TEXT NOT NULL,
     spec          JSONB NOT NULL,
     provider_kind TEXT NOT NULL DEFAULT 'local',
     index_version INT  NOT NULL DEFAULT 1,
     active        BOOLEAN NOT NULL DEFAULT true,
     created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
   )`,
  `ALTER TABLE ragtrial.models ADD COLUMN IF NOT EXISTS dims INT NOT NULL DEFAULT 256`,
  `ALTER TABLE ragtrial.models ADD COLUMN IF NOT EXISTS manifest JSONB`,
  `CREATE TABLE IF NOT EXISTS ragtrial.documents (
     repo          TEXT NOT NULL,
     branch        TEXT NOT NULL,
     doc_path      TEXT NOT NULL,
     doc_sha256    TEXT NOT NULL,
     content_bytes INT NOT NULL DEFAULT 0,
     chunk_count   INT NOT NULL DEFAULT 0,
     state         TEXT NOT NULL DEFAULT 'active',
     model_id      TEXT NOT NULL,
     model_digest  TEXT NOT NULL,
     index_version INT NOT NULL,
     object_key    TEXT,
     ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
     updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     PRIMARY KEY (repo, branch, doc_path)
   )`,
  `CREATE TABLE IF NOT EXISTS ragtrial.chunks (
     chunk_id      BIGSERIAL PRIMARY KEY,
     repo          TEXT NOT NULL,
     branch        TEXT NOT NULL,
     doc_path      TEXT NOT NULL,
     doc_sha256    TEXT NOT NULL,
     chunk_index   INT NOT NULL,
     chunk_sha256  TEXT NOT NULL,
     para_index    INT NOT NULL,
     line_start    INT,
     line_end      INT,
     char_start    INT,
     char_end      INT,
     text          TEXT NOT NULL,
     embedding     vector(256),
     model_id      TEXT NOT NULL,
     model_digest  TEXT NOT NULL,
     index_version INT NOT NULL,
     created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     UNIQUE (repo, branch, doc_path, chunk_index, index_version)
   )`,
  `CREATE INDEX IF NOT EXISTS ragtrial_chunks_scope_idx
     ON ragtrial.chunks (repo, branch, index_version)`,
  `CREATE TABLE IF NOT EXISTS ragtrial.chunks_semantic (
     chunk_id      BIGSERIAL PRIMARY KEY,
     repo          TEXT NOT NULL,
     branch        TEXT NOT NULL,
     doc_path      TEXT NOT NULL,
     doc_sha256    TEXT NOT NULL,
     chunk_index   INT NOT NULL,
     chunk_sha256  TEXT NOT NULL,
     para_index    INT NOT NULL,
     line_start    INT,
     line_end      INT,
     char_start    INT,
     char_end      INT,
     text          TEXT NOT NULL,
     embedding     vector(1024),
     model_id      TEXT NOT NULL,
     model_digest  TEXT NOT NULL,
     index_version INT NOT NULL,
     created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     UNIQUE (repo, branch, doc_path, chunk_index, index_version)
   )`,
  `CREATE INDEX IF NOT EXISTS ragtrial_chunks_semantic_scope_idx
     ON ragtrial.chunks_semantic (repo, branch, index_version)`,
  // 768 维语义表（e5-base-v2 等；与 1024 表并行，路由按 models.dims 白名单）
  `CREATE TABLE IF NOT EXISTS ragtrial.chunks_semantic_768 (
     chunk_id      BIGSERIAL PRIMARY KEY,
     repo          TEXT NOT NULL,
     branch        TEXT NOT NULL,
     doc_path      TEXT NOT NULL,
     doc_sha256    TEXT NOT NULL,
     chunk_index   INT NOT NULL,
     chunk_sha256  TEXT NOT NULL,
     para_index    INT NOT NULL,
     line_start    INT,
     line_end      INT,
     char_start    INT,
     char_end      INT,
     text          TEXT NOT NULL,
     embedding     vector(768),
     model_id      TEXT NOT NULL,
     model_digest  TEXT NOT NULL,
     index_version INT NOT NULL,
     created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     UNIQUE (repo, branch, doc_path, chunk_index, index_version)
   )`,
  `CREATE INDEX IF NOT EXISTS ragtrial_chunks_semantic768_scope_idx
     ON ragtrial.chunks_semantic_768 (repo, branch, index_version)`,
  `CREATE TABLE IF NOT EXISTS ragtrial.query_log (
     seq            BIGSERIAL PRIMARY KEY,
     actor          TEXT NOT NULL,
     state          TEXT NOT NULL,
     repo           TEXT,
     branch         TEXT,
     model_id       TEXT,
     model_digest   TEXT,
     k              INT,
     latency_ms     INT NOT NULL,
     hits           INT NOT NULL DEFAULT 0,
     cited_hits     INT NOT NULL DEFAULT 0,
     dropped_uncited INT NOT NULL DEFAULT 0,
     query_text     TEXT,
     detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
     created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
   )`,
  `CREATE TABLE IF NOT EXISTS ragtrial.audit_events (
     seq        BIGSERIAL PRIMARY KEY,
     kind       TEXT NOT NULL,
     actor      TEXT NOT NULL,
     repo       TEXT,
     branch     TEXT,
     detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
     created_at TIMESTAMPTZ NOT NULL DEFAULT now()
   )`,
  `CREATE INDEX IF NOT EXISTS ragtrial_audit_kind_idx ON ragtrial.audit_events (kind, seq)`,
  `CREATE TABLE IF NOT EXISTS ragtrial.eval_runs (
     eval_id    TEXT PRIMARY KEY,
     qa_set     TEXT NOT NULL,
     model_id   TEXT NOT NULL,
     model_digest TEXT NOT NULL,
     k          INT NOT NULL,
     total      INT NOT NULL,
     hit_at_k   INT NOT NULL,
     recall_at_k NUMERIC(6,4) NOT NULL,
     detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
     created_at TIMESTAMPTZ NOT NULL DEFAULT now()
   )`,
  // ── 持久任务队列（PRODUCTION_RAG_READINESS）──
  // 幂等：dedupe_key = sha256(kind|repo|branch|doc_path|content_sha256|model_id) UNIQUE。
  // 重试：attempts/max_attempts + next_run_at 退避；超时/崩溃恢复：heartbeat_at 过期
  // 的 running 行可被 claim 重占（stale reclaim）；死信：attempts 用尽 → state='dead'。
  `CREATE TABLE IF NOT EXISTS ragtrial.jobs (
     job_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     kind          TEXT NOT NULL,
     repo          TEXT NOT NULL,
     branch        TEXT NOT NULL,
     doc_path      TEXT,
     content_sha256 TEXT,
     model_id      TEXT NOT NULL,
     dedupe_key    TEXT NOT NULL UNIQUE,
     payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
     state         TEXT NOT NULL DEFAULT 'queued',
     attempts      INT NOT NULL DEFAULT 0,
     max_attempts  INT NOT NULL DEFAULT 5,
     timeout_ms    INT NOT NULL DEFAULT 120000,
     heartbeat_at  TIMESTAMPTZ,
     next_run_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
     locked_by     TEXT,
     locked_at     TIMESTAMPTZ,
     last_error    TEXT,
     created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
     done_at       TIMESTAMPTZ
   )`,
  `CREATE INDEX IF NOT EXISTS ragtrial_jobs_state_idx
     ON ragtrial.jobs (state, next_run_at)`,
  `ALTER TABLE ragtrial.jobs ADD COLUMN IF NOT EXISTS result JSONB`,
];

export const RETAINED_INDEX_VERSIONS = 2;

// 维度 → 物理表白名单（其它维度一律 dimension_mismatch fail-closed）
export const CHUNK_TABLES = { 256: 'ragtrial.chunks', 768: 'ragtrial.chunks_semantic_768', 1024: 'ragtrial.chunks_semantic' };
export function chunkTableFor(dims) {
  const t = CHUNK_TABLES[Number(dims)];
  if (!t) {
    const e = new Error(`unsupported embedding dims=${dims}（白名单 ${Object.keys(CHUNK_TABLES).join('/')}）`);
    e.kind = 'dimension_mismatch';
    throw e;
  }
  return t;
}
