-- 004_findings_validations.sql — 发现与验证持久化(最小闭环)
-- 契约基线: DATA-ARCHITECTURE-PG.md @ caf6909 §5.3 run.findings/finding_validations
-- 形式:无 IF NOT EXISTS——由 apply_migrations.py 跟踪幂等。

CREATE TABLE IF NOT EXISTS run.findings (
  finding_id  TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES run.runs(run_id),
  finding_key TEXT NOT NULL,
  source_stage TEXT NOT NULL,
  category TEXT,
  severity TEXT CHECK (severity IN ('HIGH','MEDIUM','LOW','INFO')),
  confidence TEXT CHECK (confidence IN ('HIGH','MEDIUM','LOW')),
  title TEXT NOT NULL,
  path TEXT,
  side TEXT CHECK (side IN ('old','new','file')),
  line INTEGER,
  evidence_sha256 TEXT,
  evidence_text TEXT,
  sources_json JSONB,
  status TEXT NOT NULL DEFAULT 'AGGREGATED' CHECK (status IN
              ('AGGREGATED','CONFIRMED','REFUTED','INCONCLUSIVE','PATCHED')),
  data_mode TEXT NOT NULL DEFAULT 'fixture',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, finding_key)
);

CREATE TABLE IF NOT EXISTS run.finding_validations (
  finding_id TEXT PRIMARY KEY REFERENCES run.findings(finding_id),
  verdict TEXT NOT NULL CHECK (verdict IN ('CONFIRMED','REFUTED','INCONCLUSIVE')),
  evidence_path TEXT,
  reason TEXT,
  anchor_status TEXT CHECK (anchor_status IN
      ('in_diff','outside_diff','file_level','undeterminable')),
  decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
