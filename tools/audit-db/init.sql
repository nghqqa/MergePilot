-- MergePilot 审计库 schema(PolarDB-PG / PostgreSQL 兼容)
-- 把 PR 审修闭环的 agent / task / finding / decision / 审计事件结构化沉淀,可查询、可审计。

-- pgvector:经验沉淀(RAG)向量列(PolarDB-PG 同样支持 vector 扩展)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agents (
  name        TEXT PRIMARY KEY,
  role        TEXT NOT NULL,
  runtime     TEXT,
  registered_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id     TEXT PRIMARY KEY,          -- e.g. gh-pr1-review / rollback-demo
  repo        TEXT NOT NULL,             -- nghqqa/mergepilot-test
  pr_number   INT,
  pr_url      TEXT,
  branch      TEXT,
  type        TEXT,                       -- review/fix/verify/merge/rollback
  status      TEXT,                       -- pending/done/failed
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS findings (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT REFERENCES tasks(task_id),
  finding_id  TEXT,                       -- F1
  category    TEXT,                       -- security/quality/...
  severity    TEXT,                       -- critical/L1/...
  risk_level  TEXT,                       -- L0/L1/L2
  file        TEXT,
  line        INT,
  description TEXT,
  source      TEXT,                       -- sast-scan / manual
  created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decisions (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT REFERENCES tasks(task_id),
  verdict     TEXT,                       -- PASS/FAIL/MERGE/HOLD/REJECT/ROLLBACK
  action      TEXT,                       -- auto-fix/merge/rollback/needs-approval/close-pr
  decided_by  TEXT,                       -- verifier/manager/admin/system
  reason      TEXT,
  pr_url      TEXT,
  commit_sha  TEXT,
  decided_at  TIMESTAMPTZ DEFAULT now()
);

-- 不可变审计事件流:闭环每一步(review/fix/verify/merge/rollback/close_pr)都追加一条
CREATE TABLE IF NOT EXISTS audit_events (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT,
  agent       TEXT,                       -- reviewer/fixer/verifier/manager/system
  action      TEXT,                       -- review/fix/verify/merge/rollback/close_pr/...
  target      TEXT,                       -- repo/branch/file/pr
  detail      TEXT,                       -- 说明或 JSON
  sha         TEXT,                       -- 相关 git commit sha
  via         TEXT,                       -- github-mcp / sast-scan / matrix / pg
  ts          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_task ON audit_events(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_find_task  ON findings(task_id);
CREATE INDEX IF NOT EXISTS idx_dec_task   ON decisions(task_id);

-- 经验沉淀知识库(RAG):每条 = 一个历史 finding + 其修复,带向量嵌入
-- 维度 384 对应 BAAI/bge-small-en-v1.5(可在 embed 脚本里替换为中文优化模型,迁移=改一处)
CREATE TABLE IF NOT EXISTS knowledge (
  id          BIGSERIAL PRIMARY KEY,
  task_id     TEXT,
  finding_id  TEXT,
  category    TEXT,
  severity    TEXT,
  issue       TEXT,                       -- 问题描述(用于检索 + 喂给 agent)
  fix         TEXT,                       -- 修复方案(召回后直接复用)
  file        TEXT,
  source      TEXT,                       -- sast-scan / manual
  embedding   vector(384),
  created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_knowledge_vec ON knowledge USING ivfflat (embedding vector_cosine_ops) WITH (lists = 4);
