-- 003_run_domain.sql — 审查目标/执行/阶段/事件(PostgreSQL,最小纵向闭环)
-- 契约基线: DATA-ARCHITECTURE-PG.md @ caf6909 §3/§5.3(targets+runs 分离、
--           request_key 幂等、exec_seq target 行锁分配、活跃部分唯一、INSERT-only)
-- 范围: 最小闭环 = target → run → stages → run_events → 读模型。
--       findings/validations/attempts/manifests 等随后续工作包建(列依赖已在设计)。
-- 偏离(登记): delivery_id 不加 FK(ingress.github_deliveries 属共享库,
--             隔离实例无该表;迁移统一时由设计窗口补齐);knowledge_manifest_id 同理。

CREATE SCHEMA IF NOT EXISTS run;

CREATE TABLE run.repos (
  repo_id  TEXT PRIMARY KEY                            -- 'owner/name'
);

CREATE TABLE run.targets (
  target_id  TEXT PRIMARY KEY,                         -- 'tgt-' + sha256(canon)[:20]
  repo_id    TEXT NOT NULL REFERENCES run.repos(repo_id),
  pr_number  INTEGER NOT NULL CHECK (pr_number >= 1),
  head_sha   TEXT NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
  base_sha   TEXT CHECK (base_sha ~ '^[0-9a-f]{40}$'),
  first_delivery_id TEXT,                              -- 偏离:无 FK(共享 ingress 表不在隔离实例;统一迁移时补)
  latest_run_id TEXT,                                  -- 冗余指针(最新 execution)
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (repo_id, pr_number, head_sha)
);

CREATE TABLE run.runs (
  run_id       TEXT PRIMARY KEY,                       -- 'gh-' + sha256(canon)[:24]
  target_id    TEXT NOT NULL REFERENCES run.targets(target_id),
  delivery_id  TEXT,                                   -- 无 UNIQUE:delivery 1:N runs
  exec_seq     INTEGER NOT NULL CHECK (exec_seq >= 1),
  chain        TEXT NOT NULL CHECK (chain IN ('legacy','v3')),
  run_class    TEXT NOT NULL CHECK (run_class IN ('execution','evidence')),
  mode         TEXT CHECK (mode IN ('off','shadow','on','legacy','fixture')),
  trigger_kind TEXT NOT NULL DEFAULT 'webhook'
               CHECK (trigger_kind IN ('webhook','manual_rerun')),
  triggered_by TEXT,
  repo_id      TEXT NOT NULL REFERENCES run.repos(repo_id),
  pr_number    INTEGER NOT NULL,
  head_sha     TEXT NOT NULL,
  base_sha     TEXT,
  risk_tier    TEXT CHECK (risk_tier IN ('TRIVIAL','LITE','FULL')),
  risk_json    JSONB,
  plan_json    JSONB,
  outcome      TEXT,
  coverage_missing JSONB,
  manifest_sha256 TEXT,
  evidence_path TEXT,
  superseded_by_run_id TEXT REFERENCES run.runs(run_id),
  claim_id     TEXT,
  claimed_at   TIMESTAMPTZ,
  status       TEXT NOT NULL DEFAULT 'PENDING'
               CHECK (status IN ('PENDING','RUNNING','SUPERSEDED',
                                 'SUCCEEDED','FAILED','CANCELLED')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (target_id, chain, run_class, exec_seq),
  request_key TEXT NOT NULL,
  UNIQUE (target_id, chain, run_class, request_key)
);

CREATE UNIQUE INDEX uq_runs_one_active
  ON run.runs (target_id, chain, run_class)
  WHERE status IN ('PENDING','RUNNING');

CREATE INDEX idx_runs_delivery ON run.runs (delivery_id);
CREATE INDEX idx_runs_pr ON run.runs (repo_id, pr_number, created_at DESC);

CREATE TABLE run.stages (
  run_id   TEXT NOT NULL REFERENCES run.runs(run_id),
  stage    TEXT NOT NULL,
  status   TEXT NOT NULL CHECK (status IN
             ('PENDING','RUNNING','SUCCEEDED','FAILED','TIMEOUT','SKIPPED',
              'NOT_APPLICABLE','CANCELLED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error    TEXT,
  detail   TEXT,
  started_at TIMESTAMPTZ,
  ended_at   TIMESTAMPTZ,
  PRIMARY KEY (run_id, stage)
);

CREATE TABLE run.run_events (
  id         BIGSERIAL PRIMARY KEY,
  run_id     TEXT NOT NULL REFERENCES run.runs(run_id),
  event_type TEXT NOT NULL,
  payload    JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
