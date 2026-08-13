# ISOLATED_LIVE Phase 2 Implementation

**Status**: P2 code implementation candidate — local review candidate, not pushed, not merged.
**Branch**: `feat/isolated-live-p2-postgres`

This is a local implementation candidate for review. It has **not** been pushed
to the remote, is not on an open PR, and is not merged. Nothing here is tagged
or released. This document states what the candidate actually does, and just as
importantly, what it deliberately does not do. It avoids overclaiming.

> **MergePilot-Test isolated verification has NOT been performed.** The source
> is tested only against in-memory fakes (no real PostgreSQL / PolarDB-PG
> connection). Any claim that it works against the real audit database is out
> of scope for this candidate.

## What this is

Phase 2 adds a second `SnapshotSource` implementation to the ISOLATED_LIVE
console: `PostgresSnapshotSource` (`tools/demo_console/postgres_source.py`).
It reads a single run's rows from the MergePilot audit/state database and
assembles a `mergepilot.demo-bundle.v1` bundle with
`demo_mode="ISOLATED_LIVE"`, which then flows through the unchanged P1 poller
and `/api/live/*` endpoints.

It is **strictly read-only**: it verifies the session is read-only and points
at the expected database/role, opens a `REPEATABLE READ READ ONLY` transaction,
issues only `SELECT` queries, then `ROLLBACK`s and closes the connection. The
DSN is treated as a secret and never appears in `repr`, `str`, exceptions, or
logs.

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
  `transaction_read_only`, and `default_transaction_read_only` before any data
  query. Mismatches raise `IdentityCheckError` (`WRONG_DATABASE` / `WRONG_ROLE`
  / `NOT_READ_ONLY`) and close the connection.
- **Bounded read-only transaction**: `BEGIN TRANSACTION ISOLATION LEVEL
  REPEATABLE READ READ ONLY` + `SET LOCAL statement_timeout` / `lock_timeout`
  / `idle_in_transaction_session_timeout` (default 10s).
- **Six parameterized read queries** (all `%s` placeholders; `run_id` never
  interpolated into SQL text): `task_runs`, `stage_events`,
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
- **DSN secrecy**: `__repr__`/`__str__` expose only public identity; all error
  strings pass through `_sanitize_text`, which redacts `password=...` to
  `password=<REDACTED>`.
- **Connection lifecycle**: `ROLLBACK` + `close()` on success, on error, and in
  a `finally` block — no idle-in-transaction residue is left on any path.
- **Mock-based test suite** (`tests/demo_console/test_postgres_source.py`,
  37 tests) using `FakeCursor` / `FakeConnection` — no real DB required.

## NOT implemented

- **MergePilot-Test isolated verification — NOT performed.** The source is
  exercised only by mock fakes; it has never been run against a real
  PolarDB-PG / PostgreSQL audit database. Verifying it end-to-end against
  `MergePilot-Test` is explicitly out of scope for this candidate.
- **`serve.py` / `preflight.py` CLI + preflight wiring** for a DB source. The
  P1 CLI still selects a `FILE_FIXTURE` source via `--source-file`. Wiring P2
  into the CLI requires a `pg_*` source-locality path in preflight (a DB source
  is not a local file and must not be validated by the `VERIFIED_LOCAL`
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
| `tools/demo_console/postgres_source.py` | **New.** `PostgresSnapshotSource` + sanitized error hierarchy. |
| `tests/demo_console/test_postgres_source.py` | **New.** 37 mock-based tests (identity, SQL safety, assembly, status, connection handling, regression). |
| `docs/ISOLATED-LIVE-P2-Postgres-Design.md` | **New.** Query-source matrix, driver choice, read-only gate, SQL safety, assembly rules, status/CLI extensions, limitations. |
| `docs/ISOLATED-LIVE-P2-Implementation.md` | **New.** This document. |

No existing source files were modified. No files under `evidence/`, `samples/`,
or any frozen path were touched.

## Test results

```
tests/demo_console/test_postgres_source.py ......... 37 tests, OK
tests/demo_console/test_isolated_live.py + test_demo_console.py ... 138 tests, OK (1 skipped)
```

The new suite introduces `FakeCursor` (returns canned rows for SQL-fragment
keys, records every executed statement) and `FakeConnection` (hands out
`FakeCursor`s, tracks `closed` / `rollback_called` / `commit_called` /
`executed`). `psycopg2` is monkeypatched via `sys.modules` so the suite runs on
any host.

## How it is used (once wired)

```python
from postgres_source import PostgresSnapshotSource
from live_poller import LivePoller

src = PostgresSnapshotSource(
    dsn=os.environ["MERGEPILOT_PG_DSN"],      # secret; never log
    run_id="run-abc",
    expected_database="mergepilot_audit",
    expected_role="mergepilot_readonly",
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
- DSN is a secret: never in `repr`/`str`/exceptions/logs; redacted on re-raise.
- `run_id` validated against `^[a-zA-Z0-9_-]+$`; every query parameterized.
- Read-only identity gate enforced before any data query.
- `statement_timeout` / `lock_timeout` / `idle_in_transaction_session_timeout`
  bounded to `query_timeout_seconds`.
- Transaction always `ROLLBACK` (never `COMMIT`); connection always `close()`d.
- Mode isolation: `demo_mode="ISOLATED_LIVE"` enforced by the poller.
- `runtime_consumes_rag_context=false`, `secret_leaks=0`, RAG boundary
  (`adopted=False`, `untrusted=True`) always preserved.
- No new network exposure: P2 reuses the P1 IPv4-loopback-only server and
  `/api/live/*` endpoints; no new bind surface.
