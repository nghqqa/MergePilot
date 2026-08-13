# ISOLATED_LIVE Phase 2 Implementation

**Status**: P2 code implementation candidate — local review candidate, not pushed, not merged.
**Branch**: `feat/isolated-live-p2-postgres`
**Commit chain**: `6bfa731` → `9ab7fd6` → `f0cad7a` → (new F: ACL/privilege/contract/error-hardening pass)

This is a local implementation candidate for review. It has **not** been pushed
to the remote, is not on an open PR, and is not merged. Nothing here is tagged
or released. This document states what the candidate actually does, and just as
importantly, what it deliberately does not do. It avoids overclaiming.

> **MergePilot-Test isolated verification = NOT_PERFORMED.** The source is
> tested only against in-memory fakes and static `.sql` parsing (no real
> PostgreSQL / PolarDB-PG connection). The ephemeral live-DB tests are gated on
> `EPHEMERAL_PG_DSN` and report NOT_EXECUTED when it is unset. Any claim that it
> works against the real audit database is out of scope for this candidate.

## What this is

Phase 2 adds a second `SnapshotSource` implementation to the ISOLATED_LIVE
console: `PostgresSnapshotSource` (`tools/demo_console/postgres_source.py`).
It reads a single run's rows from the MergePilot audit/state database and
assembles a `mergepilot.demo-bundle.v1` bundle with
`demo_mode="ISOLATED_LIVE"`, which then flows through the unchanged P1 poller
and `/api/live/*` endpoints.

It is **strictly read-only**: it verifies the session is read-only and points
at the expected database/role, probes per-table privileges over every queried
table, opens a `REPEATABLE READ READ ONLY` transaction, issues only `SELECT`
queries, then `ROLLBACK`s and closes the connection. The DSN is treated as a
secret and never appears in `repr`, `str`, exceptions, or logs; on any
psycopg2/libpq error the re-raised message carries only the stable code + type
name (the raw libpq message is never included).

P2 is **additive** to P1. It does not modify the frozen REPLAY path, the frozen
evidence/samples, or the P1 server/poller/preflight contracts. The design is
specified in `docs/ISOLATED-LIVE-P2-Postgres-Design.md`.

## Implemented

- **`PostgresSnapshotSource(SnapshotSource)`** with `kind="POSTGRES_ISOLATED"`
  and `read_only=True`, satisfying the frozen P1 `SnapshotSource` interface
  (`read_snapshot()->bytes`, `kind`, `read_only`).
- **Lazy `psycopg2` import** inside `read_snapshot()`, so REPLAY / FILE_FIXTURE
  deployments never need the driver. Missing driver → stable
  `PSYCOPG2_MISSING` error code.
- **Read-only identity gate**: verifies `current_database()`, `current_user`,
  `transaction_read_only`, `default_transaction_read_only`, server identity
  (address/port/application_name/version), schema/search_path, required-table
  catalog presence, and the trusted environment marker before any data query.
  Mismatches raise `IdentityCheckError` (`WRONG_DATABASE` / `WRONG_ROLE` /
  `NOT_READ_ONLY` / `WRONG_SERVER` / `SCHEMA_INCOMPATIBLE` /
  `ENVIRONMENT_ID_*`) and close the connection.
- **Per-table privilege probe over ALL queried tables**: for each of
  `task_runs`, `stage_runs`, `revision_bindings`, `run_pr_bindings`,
  `rollback_runs`, `environment_identity`, the source asserts via
  parameterized `has_table_privilege(current_user, %s, ...)` queries that
  SELECT is present and INSERT/UPDATE/DELETE/TRUNCATE are absent. Any write
  privilege on any queried table → `WRONG_ROLE` fail-closed.
- **`REQUIRED_QUERY_COLUMNS`**: a precise per-table mapping of only the columns
  each query actually references. The runtime `information_schema.columns`
  probe checks exactly these columns (not the full migration column set), so a
  migration adding a column the source does not read cannot fail the read.
- **Bounded read-only transaction**: `BEGIN TRANSACTION ISOLATION LEVEL
  REPEATABLE READ READ ONLY` + `SET LOCAL statement_timeout` / `lock_timeout`
  / `idle_in_transaction_session_timeout` (default 10s).
- **Parameterized read queries** (all `%s` placeholders; `run_id` never
  interpolated into SQL text): `task_runs`, `stage_runs`, `stage_events`,
  `revision_bindings`, `run_pr_bindings`, `mcp_calls`, `rollback_runs`, and an
  `audit_events` aggregate summary.
- **`run_id` allowlist**: validated against `^[a-zA-Z0-9_-]+$` in the
  constructor (`RunIdError` / `RUN_ID_INVALID`); injection attempts are
  rejected before any query runs.
- **No write/DDL SQL**: only `SELECT`, `BEGIN`, `SET LOCAL`, and `ROLLBACK`.
- **DemoBundle assembly** with `demo_mode="ISOLATED_LIVE"` and a
  `bundle_sha256` computed by the shared `integrity.compute_bundle_sha256`
  (single authoritative digest). Assembled bundles pass
  `validate_bundle(bundle, expected_mode="ISOLATED_LIVE")` with zero errors.
- **Honest status mapping**: `task_runs.status` mapped through a closed
  allowlist; unknown / missing statuses map to `UNKNOWN`, **never** `MERGED`.
- **RAG boundary preserved**: `rag_advisories` reports both roles with
  `adopted=False`, `untrusted=True`, `status="not_measured"`, `hit_count=0`.
- **DSN secrecy / no raw libpq text**: `__repr__`/`__str__` expose only public
  identity; on ANY `psycopg2`/libpq error the re-raised message carries ONLY
  the stable code + exception type name — the raw libpq message (which can
  echo the connection string on connect failure) is NEVER included, not even
  after redaction. A negative test feeds a DSN like
  `postgresql://user:SUPERSECRET@host/db` and asserts the message contains
  none of `SUPERSECRET`, `postgresql://`, `user:`.
- **`environment_identity` migration ACL**: `001_environment_identity.sql`
  does `REVOKE ALL ON environment_identity FROM PUBLIC` +
  `GRANT SELECT ON environment_identity TO mergepilot_reader`. PUBLIC has no
  privileges; the canonical ISOLATED_LIVE viewer role `mergepilot_reader` has
  SELECT only.
- **Connection lifecycle**: `ROLLBACK` + `close()` on success, on error, and in
  a `finally` block — no idle-in-transaction residue is left on any path.
- **Test suite** (`tests/demo_console/test_postgres_source.py`, 157 tests of
  which 5 are ephemeral/NOT_EXECUTED) using `FakeCursor` / `FakeConnection` —
  no real DB required for the mock and static tests.

## NOT implemented

- **MergePilot-Test isolated verification — NOT_PERFORMED.** The source is
  exercised only by mock fakes and static `.sql` parsing; it has never been run
  against a real PolarDB-PG / PostgreSQL audit database. The ephemeral live-DB
  tests (`TestEphemeralMigrationProbe`) are gated on `EPHEMERAL_PG_DSN` and
  report NOT_EXECUTED when that variable is unset (the default, including CI).
  Real PostgreSQL verification = NOT_PERFORMED.
- **`serve.py` / `preflight.py` CLI + preflight wiring** for a DB source. CLI
  status: the `--source-kind` flag accepts `file`/`postgres`, and preflight
  validates a `postgres` source config (DSN-from-env, run_id, expected_*), but
  a live `serve.py` run against a real DB is NOT wired. Wiring P2 into the
  running server requires a `pg_*` source-locality path in preflight (a DB
  source is not a local file and must not be validated by the `VERIFIED_LOCAL`
  drive-type check) and an env-supplied DSN (never a CLI argument, where it
  would leak into shell history / process listings). This is the next step.
- **Production database access.** None. The console does not touch any
  production datastore; this candidate defines the read path but has not been
  connected to a production database.
- **Production management dashboard.** None. There is no management UI.
- **Dynamic (SPA) pages** that poll `/api/live/*` at runtime. The served pages
  remain the static, frozen P1 REPLAY HTML.
- **Per-skill verdict / inline finding surfacing.** `skill_invocations`,
  `envelope_store`, and the SKILL registry chain are not read; `findings`,
  `fixes`, `spans`, and `verifier_result` carry honest empty / UNKNOWN markers.
- **M8.** Not defined. No M8 tag or release exists.

## Changed files

| File | Purpose |
|------|---------|
| `tools/demo_console/postgres_source.py` | `PostgresSnapshotSource` + sanitized error hierarchy. Per-table privilege probe (6 tables), `REQUIRED_QUERY_COLUMNS`, no-raw-libpq-text error handling. |
| `tools/demo_console/migrations/001_environment_identity.sql` | `environment_identity` marker table. ACL: `REVOKE ALL FROM PUBLIC` + `GRANT SELECT TO mergepilot_reader`. |
| `tests/demo_console/test_postgres_source.py` | 157 tests: mock-based + static `.sql`-parsing contract tests + `EPHEMERAL_PG_DSN`-gated ephemeral (NOT_EXECUTED). |
| `tests/demo_console/test_isolated_live.py` | Write-method stability test: single-attempt (no retry masking), `retry_count=0` asserted; `ConnectionAbortedError` surfaced as a failure diagnostic. |
| `docs/ISOLATED-LIVE-P2-Postgres-Design.md` | Query-source matrix, driver choice, read-only gate, SQL safety, assembly rules, status/CLI extensions, limitations. |
| `docs/ISOLATED-LIVE-P2-Implementation.md` | This document. |

No files under `evidence/`, `samples/`, or any frozen path were touched.

## Test results

```
python -I -B -W error::ResourceWarning -m unittest discover \
    -s tests/demo_console -p "test_*.py"
Ran 296 tests in ~14s
OK (skipped=6)
```

Per-file: `test_postgres_source.py` 157 (5 ephemeral NOT_EXECUTED),
`test_isolated_live.py` 106 (1 skipped), `test_demo_console.py` 33.

Test classification (mutually exclusive):

- **Mock-based** (`FakeCursor`/`FakeConnection`, no real DB): the bulk of
  `test_postgres_source.py` — identity, SQL safety, assembly, status,
  connection handling, privilege probes, runtime catalog, regression.
- **Static** (parse actual `.sql` files, no DB): `TestMigrationContractFiles`
  reads `m3_state.sql`/`m3b_b4.sql`/`m3c_state.sql`/`m4f1_state.sql`/
  `001_environment_identity.sql`, extracts CREATE TABLE + ALTER TABLE ADD
  COLUMN lists, and asserts `REQUIRED_QUERY_COLUMNS` is a subset.
- **Ephemeral** (gated on `EPHEMERAL_PG_DSN`, NOT_EXECUTED when unset):
  `TestEphemeralMigrationProbe` — 5 tests, all skipped with reason
  "EPHEMERAL_PG_DSN not configured; ephemeral test NOT_EXECUTED".

`psycopg2` is monkeypatched via `sys.modules` so the mock/static suite runs on
any host. Real PostgreSQL verification = NOT_PERFORMED.

## How it is used (once wired)

```python
from postgres_source import PostgresSnapshotSource
from live_poller import LivePoller

src = PostgresSnapshotSource(
    dsn=os.environ["MERGEPILOT_PG_DSN"],      # secret; never log
    run_id="run-abc",
    expected_database="mergepilot_audit",
    expected_role="mergepilot_reader",
    expected_environment_id="mergepilot-test-env",
    expected_server_addresses=["127.0.0.1"],
    expected_server_port=5432,
    expected_application_name="mergepilot_viewer",
    query_timeout_seconds=10.0,
)
poller = LivePoller(src, poll_interval=2.0, expected_mode="ISOLATED_LIVE")
poller.initial_load()
poller.start()
```

The poller's `get_view()` then reports `source_kind="POSTGRES_ISOLATED"` and
`source_read_only=True`, surfaced unchanged through the P1 status API.

## Boundaries

- `read_only=True` always; only `SELECT` inside a `READ ONLY` transaction.
- DSN is a secret: never in `repr`/`str`/exceptions/logs; on any psycopg2/libpq
  error the re-raised message carries ONLY the stable code + type name (raw
  libpq text never included).
- `run_id` validated against `^[a-zA-Z0-9_-]+$`; every query parameterized.
- Read-only identity gate enforced before any data query; per-table privilege
  probe over all 6 queried tables (SELECT required, writes forbidden).
- `REQUIRED_QUERY_COLUMNS`: runtime column probe checks only the columns each
  query references (narrower than the full migration column set).
- `environment_identity` ACL: `REVOKE ALL FROM PUBLIC`, `GRANT SELECT TO
  mergepilot_reader` (PUBLIC has no privileges).
- `statement_timeout` / `lock_timeout` / `idle_in_transaction_session_timeout`
  bounded to `query_timeout_seconds`.
- Transaction always `ROLLBACK` (never `COMMIT`); connection always `close()`d.
- Mode isolation: `demo_mode="ISOLATED_LIVE"` enforced by the poller.
- `runtime_consumes_rag_context=false`, `secret_leaks=0`, RAG boundary
  (`adopted=False`, `untrusted=True`) always preserved.
- Windows stability: write-method requests are single-attempt (`retry_count=0`);
  `ConnectionAbortedError` is caught in `serve.py` to return 405 cleanly and is
  surfaced (not retried/masked) if it escapes.
- No new network exposure: P2 reuses the P1 IPv4-loopback-only server and
  `/api/live/*` endpoints; no new bind surface.
