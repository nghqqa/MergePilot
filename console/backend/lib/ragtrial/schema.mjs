// ragtrial/schema.mjs — RAG trial PG schema（幂等初始化）。
//
// 命名空间 ragtrial 与 fxv 完全隔离；本栈 PG 为独立实例（local-rag-trial 栈），
// 绝不挂接 promote/staging/生产卷。
//
// 绑定模型：chunks 每行携带 repo/branch/doc_path/doc_sha256/chunk 指纹/
// model_id/model_digest/index_version —— 检索结果因此可给出完整引用链。
// 版本保留：重灌新 index_version 时保留旧版本行（最近 2 个版本），
// 供 index rollback 演练；查询只命中 active model 的 digest+version。

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
];

export const RETAINED_INDEX_VERSIONS = 2;
