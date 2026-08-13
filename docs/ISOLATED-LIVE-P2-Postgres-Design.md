# ISOLATED_LIVE Phase 2 — PostgreSQL Snapshot Source (Design)

**Status**: P2 code implementation candidate — local review candidate, not pushed, not merged.
**Branch**: `feat/isolated-live-p2-postgres`
**Builds on**: `docs/ISOLATED-LIVE-P1-Implementation.md` (P1 read-only console + `SnapshotSource` interface)

This document specifies the Phase 2 PostgreSQL snapshot source: a
read-only `SnapshotSource` that assembles a `mergepilot.demo-bundle.v1` bundle
on the fly from a single run's rows in the MergePilot audit/state database. It
describes the query-source matrix, the driver choice, the read-only gate, the
SQL safety rules, the DemoBundle assembly rules, the status API extensions, the
CLI extensions, and — explicitly — what is and is not measured.

P2 is **additive** to P1. It introduces one new `SnapshotSource` implementation
and does not modify the frozen REPLAY path, the frozen evidence/samples, or the
P1 server/poller/preflight contracts.

## 1. Goal

Give an operator a live, read-only view of a single in-flight or completed run
by reading that run's rows from PostgreSQL and presenting them as a DemoBundle
through the existing P1 poller + `/api/live/*` endpoints — with no writes, no
production side effects, and no new network exposure beyond P1.

## 2. Query-source matrix (DemoBundle field → PostgreSQL table)

The bundle is assembled from six read-only queries, all scoped to a single
`run_id`. Every field the source cannot truthfully populate from the DB is
recorded as an explicit NOT_MEASURED / empty marker (never fabricated).

| DemoBundle field | PostgreSQL source | Notes |
|------------------|-------------------|-------|
| `schema_version` | constant `mergepilot.demo-bundle.v1` | unchanged |
| `demo_mode` | constant `ISOLATED_LIVE` | enforced by mode isolation |
| `generated_at` | `time.gmtime()` at assembly | volatile (excluded from digest) |
| `source_commit` / `verification_commit` | NOT_MEASURED (empty) | the DB viewer has no git working copy |
| `repo` | `task_runs.repo` (fallback `revision_bindings.repo`) | |
| `pr.number` | `task_runs.pr_number` / `revision_bindings.pr_number` | |
| `pr.title` | NOT_MEASURED (empty) | title is not stored in the audit DB |
| `pr.base_sha` | `revision_bindings.base_sha` | authoritative revision cut (NOT `run_pr_bindings`) |
| `pr.head_sha` | `revision_bindings.head_sha` (fallback `run_pr_bindings.head_sha`) | |
| `run.run_id` | the requested `run_id` | validated against `^[a-zA-Z0-9_-]+$` |
| `run.trace_id` | `task_runs.trace_id` | added by M4-F1 |
| `run.entrypoint` | constant `controller.process_event` | stable default |
| `final_status` | `task_runs.status` mapped via allowlist | unknown → `UNKNOWN`, never `MERGED` |
| `workflow_stages` | `stage_events` (one per distinct `stage`) | status from `stage_events.status` |
| `agents` | derived from `stage_events` | role inferred best-effort |
| `findings` | EMPTY list | inline finding bodies are NOT exposed by the read-only DB view |
| `fixes` | EMPTY list | same boundary as `findings` |
| `verifier_result` | default `verdict=UNKNOWN` | per-invocation verdicts live in `skill_invocations`, not surfaced here |
| `rag_advisories` | two roles, `status=not_measured`, `hit_count=0` | **RAG boundary preserved**: `adopted=False`, `untrusted=True` |
| `spans` | EMPTY list | OTel span bodies are not stored in the DB |
| `rollback_events` | `rollback_runs` (parent or revert run = `run_id`) | |
| `evidence_files` | EMPTY list | the DB is the source, not files on disk |
| `secret_leaks` | constant `0` | the source emits no secrets |
| `residue` | `{gateway_audit_summary, audit_events_summary, stage_event_count}` | aggregate counts only |
| `benchmark_summary` | NOT_MEASURABLE defaults | matches REPLAY contract |
| `topology` | labels (`policy_gateway="mcp_calls"`); rest empty | |
| `bundle_sha256` | `compute_bundle_sha256(bundle)` via `integrity.py` | single authoritative digest |

### 2.1 Tables read (schema references)

- `task_runs` (m3_state + m4f1 extension: `trace_id`, `active_snapshot_id`, `skill_data_state`)
- `stage_events` (m3_state: Matrix event dedup + audit)
- `revision_bindings` (m4f1: immutable one-run-one-revision with `base_sha`/`head_sha`)
- `run_pr_bindings` (m3b_b4: PR branch identity `fix_branch`/`base_branch`)
- `mcp_calls` (m3b_policy: immutable INSERT-only gateway audit)
- `rollback_runs` (m3c: parent/revert run rollback chain)
- `audit_events` (init.sql: aggregate counts only; keyed by `task_id`)

Tables **not** read by P2 (deliberate boundaries): `stage_runs`, `dispatch_outbox`,
`envelope_store`, `run_snapshots`, `skill_job_outbox`, `skill_invocations`,
`snapshot_manifest_items`, `skill_version_registry`, `purge_requests`,
`approvals`, `policy_action_outbox`, `knowledge`. These are either internal
runtime plumbing or contain data not exposed to the read-only viewer.

## 3. Driver choice (psycopg2, lazy import)

- **Driver**: `psycopg2-binary==2.9.12`. `psycopg2-binary` is used by other
  PostgreSQL subsystems in the repository (`skills/case_retrieval`,
  `tools/policy-gateway`, `tools/rag`); `POSTGRES_ISOLATED` requires explicit
  optional installation (it is NOT pulled in automatically for REPLAY/FILE
  deployments).
- **Lazy import**: `import psycopg2` happens **inside** `read_snapshot()`, not
  at module top. REPLAY and FILE_FIXTURE deployments never execute that path,
  so they never need the driver installed. The test suite monkeypatches
  `sys.modules['psycopg2']` to a fake, so it runs on hosts without a DB.
- **Missing driver**: if the import fails, the source raises
  `PostgresSourceError("PSYCOPG2_MISSING: ...")` — a stable, machine-readable
  code that the poller surfaces via `last_error_code`.

## 4. Read-only gate checks

Before any data query, the source verifies the session identity. All four
checks must pass; any failure raises `IdentityCheckError` and closes the
connection.

```sql
SELECT current_database(),
       current_user,
       current_setting('transaction_read_only')::boolean,
       current_setting('default_transaction_read_only')::boolean
```

| Check | Required value | Failure code |
|-------|----------------|--------------|
| `current_database()` | equals `expected_database` (constructor arg) | `WRONG_DATABASE` |
| `current_user` | equals `expected_role` (constructor arg) | `WRONG_ROLE` |
| `transaction_read_only` | `true` | `NOT_READ_ONLY` |
| `default_transaction_read_only` | `true` | `NOT_READ_ONLY` |

Both read-only flags are required (defense in depth): even though the explicit
`BEGIN ... READ ONLY` would also constrain writes, the source refuses to
operate against a session whose defaults are writable.

After the identity probe passes, the source opens an explicit transaction:

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY
SET LOCAL statement_timeout             = <ms>
SET LOCAL lock_timeout                  = <ms>
SET LOCAL idle_in_transaction_session_timeout = <ms>
```

- `REPEATABLE READ` gives a stable snapshot for the multi-query read.
- `READ ONLY` is a second server-side guard against accidental writes.
- `statement_timeout` / `lock_timeout` / `idle_in_transaction_session_timeout`
  bound any single query, any lock wait, and any idle-in-transaction residue
  to `query_timeout_seconds` (default 10s).
- The transaction is always ended with `ROLLBACK` (never `COMMIT`), and the
  connection is always `close()`d — on success, on error, and in a `finally`
  block.

## 5. SQL safety rules

1. **Parameterization.** Every query that carries the `run_id` uses a `%s`
   placeholder and passes `run_id` as a parameter tuple. The `run_id` never
   appears literally in any SQL text. Verified by `TestSqlSafety`.
2. **run_id allowlist.** `run_id` is validated against `^[a-zA-Z0-9_-]+$` in
   the constructor (`RunIdError` / `RUN_ID_INVALID`). A SQL-injection string
   like `x'; DROP TABLE task_runs; --` is rejected before any query runs.
3. **No write/DDL SQL.** Only `SELECT`, `BEGIN`, `SET LOCAL`, and the final
   `ROLLBACK` are issued. `INSERT`/`UPDATE`/`DELETE`/`DROP`/`TRUNCATE`/`ALTER`/
   `CREATE`/`GRANT`/`MERGE INTO`/`CALL` are forbidden by tests (matched with
   word boundaries so column names like `UPDATED_AT` are not false positives).
4. **Bounded timeouts.** All three session timeouts are `SET LOCAL` to the
   configured ceiling, so a slow/hung query cannot hold resources.
5. **DSN secrecy.** The DSN is a secret. It is stored once in a private
   attribute and never appears in `repr`, `str`, exception messages, or logs.
   `__repr__`/`__str__` expose only `run_id`, `expected_database`,
   `expected_role`, `kind`. Any `psycopg2`/libpq error string is passed through
   `_sanitize_text`, which redacts `password=...` fragments to
   `password=<REDACTED>` before the message is re-raised.

## 6. DemoBundle assembly rules

- **Mode**: `demo_mode = "ISOLATED_LIVE"`. The poller's mode isolation
  (`expected_mode`) accepts only this value; a REPLAY bundle is rejected and
  vice versa.
- **Digest**: `bundle_sha256 = compute_bundle_sha256(bundle)` from
  `integrity.py` — the same single authoritative digest used by the builder,
  schema, preflight, and poller. The digest excludes volatile fields
  (`bundle_sha256`, `generated_at`).
- **Status mapping**: `task_runs.status` is mapped through a closed allowlist.
  Any unrecognized value (including `None` or a missing row) maps to `UNKNOWN`
  — **never** to `MERGED`. `MERGED` is only reported when the DB explicitly
  records it.
- **RAG boundary preserved**: `rag_advisories` always has both roles
  (`reviewer`, `fixer`) with `adopted=False` and `untrusted=True`, exactly as
  the schema authenticity rules require. The DB does not expose RAG hit
  contents to this viewer, so `status="not_measured"` and `hit_count=0`.
- **No fabricated findings**: `findings` and `fixes` are empty lists. The
  read-only DB view does not materialize the inline finding bodies the REPLAY
  bundle carries; empty is the truthful representation.
- **Honest NOT_MEASURED markers**: `source_commit`, `verification_commit`,
  `pr.title`, OTel `spans`, `evidence_files`, and benchmark numbers are
  empty/`NOT_MEASURABLE_WITH_CURRENT_RUNTIME` rather than invented.
- **Schema validity**: the assembled bundle passes
  `validate_bundle(bundle, expected_mode="ISOLATED_LIVE")` with zero errors,
  so it flows through the existing P1 poller unchanged.

## 7. Status API extensions

P2 requires **no change** to the P1 status contract. The status endpoint reads
source identity from the actual `SnapshotSource` via `poller.get_view()`, so a
`PostgresSnapshotSource` automatically reports:

```json
{
  "mode": "ISOLATED_LIVE",
  "source_kind": "POSTGRES_ISOLATED",
  "source_read_only": true,
  "not_production": true,
  "github_writes_enabled": false,
  "agent_control_enabled": false,
  "runtime_consumes_rag_context": false,
  "production_resource_accessed": null,
  "production_resource_access_status": "NOT_MEASURED",
  "dynamic_pages_consume_live_api": false
}
```

`source_kind = "POSTGRES_ISOLATED"` (the class attribute) is how a consumer
tells a live DB-backed snapshot apart from a `FILE_FIXTURE`. `source_read_only`
is `True` (the property returns `True` unconditionally). This is the only
contract surface P2 exercises; no new fields are added.

## 8. CLI extensions

P2 is wireable through the existing `serve.py` CLI without changing its flags.
The P1 `--source-file` flag carries a local JSON bundle path; P2 would extend
selection to a `PostgresSnapshotSource` constructed from environment-provided
identity (the DSN must never be a CLI argument, where it would leak into shell
history / process listings). The minimal integration shape:

```
# ISOLATED_LIVE + PostgreSQL (env-supplied identity, NOT a CLI flag):
export MERGEPILOT_PG_DSN='...'
python tools/demo_console/serve.py \
    --mode isolated_live \
    --pg-run-id run-abc \
    --pg-database mergepilot_audit \
    --pg-role mergepilot_readonly \
    [--pg-query-timeout 10] \
    --host 127.0.0.1 --port 8080 --poll-interval 2
```

The DSN is read from the environment, never parsed from `argv`. This CLI
extension is **part of the design** but is **not implemented in this candidate**
(see the implementation status doc): this candidate delivers the source + mock
tests + design; wiring it into `serve.py`/`preflight.py` is the next step and
must add a `pg_*` source-locality path to preflight (a DB source is not a local
file and must not be validated by the `VERIFIED_LOCAL` drive-type check).

## 9. Limitations and NOT_MEASURED fields

P2 deliberately does not measure or expose:

- **OTel spans** — stored in the observability backend, not the audit DB.
- **Inline finding/fix bodies** — the DB stores digests/summaries, not the
  inline payload the REPLAY bundle carries.
- **Per-skill verdicts** — live in `skill_invocations`, not surfaced to this
  read-only viewer (would require joining the envelope/SKILL registry chain).
- **Benchmark metrics** — come from the M7 benchmark evidence, not the DB.
- **PR title / commit SHAs for source/verification** — the DB viewer has no
  git working copy; only the revision-binding SHAs are available.
- **Production resource access** — `production_resource_accessed=null` /
  `production_resource_access_status=NOT_MEASURED`, exactly as P1: the console
  refuses production side effects but does not actively measure access.
- **Browser-side network observation** — `NOT_MEASURED` / `null`, as P1.

These are **honest absences**, recorded explicitly so a consumer cannot mistake
"not measured" for "measured and clean".

## 10. What's implemented vs. not (summary)

**Implemented (this candidate)**

- `PostgresSnapshotSource(SnapshotSource)` with `kind="POSTGRES_ISOLATED"`,
  `read_only=True`.
- Lazy `psycopg2` import; `PSYCOPG2_MISSING` error code.
- Read-only identity gate (database / user / both read-only flags).
- `REPEATABLE READ READ ONLY` transaction with `SET LOCAL` timeouts.
- Six parameterized read queries (task_runs, stage_events,
  revision_bindings, run_pr_bindings, mcp_calls, rollback_runs, audit_events).
- DemoBundle assembly with `demo_mode="ISOLATED_LIVE"` + `bundle_sha256`.
- DSN secrecy: never in `repr`/`str`/exceptions/logs; `_sanitize_text`
  redaction.
- Connection lifecycle: `ROLLBACK` + `close()` on success, error, and finally.
- Mock-based test suite (37 tests, no real DB).

**NOT implemented (this candidate)**

- `serve.py` / `preflight.py` CLI + preflight wiring for a DB source
  (requires a `pg_*` source-locality path distinct from the file
  `VERIFIED_LOCAL` check).
- MergePilot-Test isolated verification against a real PolarDB-PG instance.
- Production database access / production management dashboard.
- Dynamic (SPA) pages that poll `/api/live/*` at runtime.
- Per-skill verdict / inline finding surfacing (would require the
  `skill_invocations` + envelope chain).
- M8 — not defined.
