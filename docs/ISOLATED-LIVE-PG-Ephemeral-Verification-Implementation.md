# ISOLATED_LIVE PostgreSQL Ephemeral Verification — Implementation (Phase A)

**Status**: Phase A code implementation candidate — local review candidate, not
pushed, not merged.
**Base**: `feat/isolated-live-pg-ephemeral-design` (HEAD `6e6d5fb`)
**Created**: 2026-08-13
**Design**: `docs/ISOLATED-LIVE-PG-Ephemeral-Verification-Design.md`

This document tracks the Phase A implementation of the ISOLATED_LIVE PostgreSQL
ephemeral verification harness. Phase A is **code only**: the harness scaffolding
and unit tests exist, but no Docker container is started and no real PostgreSQL
is exercised. Execution is Phase B; evidence is Phase C.

## What is implemented (Phase A)

| Artifact | Path | Purpose |
|---|---|---|
| Package marker | `tests/isolated_live/__init__.py` | Makes the test dir importable |
| Harness module | `tests/isolated_live/ephemeral_harness.py` | Constants, command builders, seed SQL, digest, gate, validation |
| Unit tests | `tests/isolated_live/test_ephemeral_pg.py` | 9 test classes; pure unit tests, no Docker |

### Harness module (`ephemeral_harness.py`)

Constants:
- `IMAGE_DIGEST` — digest-pinned `pgvector/pgvector@sha256:a362...c6b` (matches
  the m4f1 foundation script; no floating tag).
- `AUTHORIZED_DAEMON` — `"MergePilot-Test"` (the only authorized Docker daemon;
  production `Ubuntu-22.04` is never touched).
- `CANONICAL_VIEWER_ROLE` — imported from `tools/demo_console/postgres_source.py`
  so there is exactly one source of truth for the reader role name.
- `PREREQUISITE_ROLES` — `["policy_gateway_l2 NOLOGIN",
  "mergepilot_approver NOLOGIN"]` (Phase 0, before any migration).
- `ISOLATED_LIVE_MIGRATIONS` — `["001_environment_identity.sql",
  "002_mergepilot_reader_acl.sql"]` (Phase 3).
- `MIGRATION_CHAIN` — 13 ordered `(filename, description)` pairs (9 base +
  `m4f1_state` x2 + `m4f1_hotfix_1` x2; 11 distinct files). See the count note
  below.
- `ENVIRONMENT_ID_EPHEMERAL` — `"mergepilot-test-ephemeral"`.

Functions (defined, not executed in Phase A):
- `check_execution_auth() -> dict` — two-key gate: `EPHEMERAL_PG_VERIFY=1` (the
  literal `"1"`) AND the MergePilot-Test daemon reachable via
  `wsl -u root -d MergePilot-Test docker info`. Uses array argv, never
  `shell=True`. Never raises on probe failure (fail-closed →
  `authorized=False`).
- `build_migration_commands(container, db_name, user, root_path) -> list` — one
  argv array per migration application (`docker exec -i <ctr> psql ... -f
  <path>`), with `-v ON_ERROR_STOP=1`.
- `build_prerequisite_role_sql() -> str` — idempotent Phase-0 role creation.
- `build_reader_role_sql(password) -> str` — `CREATE ROLE mergepilot_reader`
  with all privileged attributes OFF and `default_transaction_read_only = on`.
  The password is a function arg, SQL-quote-escaped, never stored on the module.
- `build_seed_sql() -> str` — deterministic INSERTs for all 5 runs
  (ok/unknown/no-rev/rollback/missing) with the `source_evidence_digest`
  pre-computed via `compute_revision_digest`.
- `build_cleanup_commands(container_name, label) -> list` — argv arrays for
  `docker rm -f` + `docker ps --filter name=` + `docker network ls/prune
  --filter label=`. Validates name and label before emitting any command.
- `validate_container_name(name) -> bool` — anchored regex +
  forbidden-substring rejection (path traversal, shell metacharacters,
  whitespace).
- `redact_secrets(text) -> str` — scrubs both `password=...` (conninfo) and
  `PASSWORD '...'` (SQL) forms.
- `measure_server_identity(admin_dsn) -> dict` — Phase-A placeholder; returns
  `{"executed": False, "reason": "NOT_EXECUTED"}`. Never opens a connection.
- `compute_revision_digest(...) -> str` — pure-stdlib mirror of the
  `bind_revision` evidence-digest algorithm (length-prefixed canonical
  concatenation, SHA256, lowercase hex).

Security invariants enforced:
- Every subprocess call uses **array arguments** (no `shell=True`). The command
  builders return lists of argv lists so callers pass them directly to
  `subprocess.run`.
- The reader-role password is a **function argument**, never a module constant,
  and is absent from `repr`/`str`/logs. `redact_secrets` scrubs it from both
  conninfo and SQL forms.
- Container names are validated before any `docker rm`; path traversal and
  shell metacharacters are rejected (defense in depth on top of argv safety).
- All file reads use `with`.
- The digest algorithm is pure-stdlib (`hashlib`); no psycopg2 import at module
  load (keeps the suite runnable without a DB driver).

### Test file (`test_ephemeral_pg.py`)

9 test classes, 83 tests total. `subprocess` is mocked everywhere; no real
Docker/WSL process is spawned.

| Class | Tests | Scope |
|---|---:|---|
| `TestExecutionGate` | 7 | env var + daemon gate (mocked subprocess) |
| `TestMigrationOrder` | 9 | chain length, idempotency rounds, ordering |
| `TestRoleBootstrap` | 11 | prerequisite + reader role SQL shape |
| `TestSeedContract` | 13 | 5-run seed satisfies DDL CHECK/FK constraints |
| `TestRevisionDigest` | 5 | canonical digest algorithm vs `bind_revision` |
| `TestCommandSafety` | 16 | argv arrays, redaction, name validation |
| `TestCleanupValidation` | 10 | container-name + label targeting |
| `TestResultClassification` | 5 | skip reasons + classification strings |
| `TestEphemeralPlaceholder` | 7 | unconditional skip; documents Phase B intent |

## Test statistics

Run with `python -m pytest tests/isolated_live/test_ephemeral_pg.py -v`:

```
83 discovered / 76 passed / 7 skipped / 0 failed
```

The 7 skips are the `TestEphemeralPlaceholder` class, which skips
unconditionally with reason `"EPHEMERAL_PG_VERIFY not configured;
NOT_EXECUTED"`. This is an honest placeholder: even when `EPHEMERAL_PG_VERIFY=1`
is set, Phase A does not implement container execution, so every placeholder
test still skips.

No real Docker or PostgreSQL is contacted by any test. Combined run with the
existing `tests/demo_console/test_postgres_source.py`:
`251 passed, 12 skipped` — no import collisions.

## What is NOT implemented (Phase B / C)

- **Docker execution**: no container is started, no `docker run`, no
  `pg_isready`. `build_migration_commands`/`build_cleanup_commands` only build
  argv arrays; nothing invokes them.
- **Real PostgreSQL verification**: no `psycopg2.connect`, no migration
  application against a live DB, no `PostgresSnapshotSource.initial_load()`
  against seeded data. `measure_server_identity` returns a placeholder.
- **Evidence output**: no `evidence/post-m7/isolated-live/...json` artifact.
  `ephemeral_postgres_verified` remains `DESIGNED, NOT_EXECUTED`.
- **`bind_revision()` producer-contract test**: the seed uses the Option B
  direct-admin INSERT fallback, so `revision_producer_contract = NOT_VERIFIED`
  for the seed path. Phase B should attempt `bind_revision()` (Option A) first.

## Count reconciliation note

**Unified terminology** (applied consistently across the design doc, this
implementation doc, the harness, and the unit tests):

- **audit-db migration applications = 13** (`init..m3c_state` = 9 base, plus
  `m4f1_state` twice and `m4f1_hotfix_1` twice for idempotency = 13).
- **distinct audit-db migration files = 11**.
- **ISOLATED_LIVE migration applications = 2** (`001`/`002`), a SEPARATE
  Phase-3 step here (`ISOLATED_LIVE_MIGRATIONS`), NOT part of `MIGRATION_CHAIN`.
- **total migration-file applications = 15** (13 audit-db + 2 ISOLATED_LIVE).
- **prerequisite roles = 2** (`policy_gateway_l2`, `mergepilot_approver`).
- **viewer role = 1** (`mergepilot_reader`).
- **executor operations = 17** (15 migrations + 2 role bootstraps).

`MIGRATION_CHAIN` holds the **13** audit-db applications. The unit tests
(`test_chain_has_thirteen_audit_db_entries`,
`test_eleven_distinct_files_present`) assert this authoritative count, matching
`tests/m4f1/run_schema_foundation.sh` (BASE=9, then m4f1 round 1/2, then hotfix
round 1/2).

## Verification classification (unchanged from design §2)

| Classification | Status after Phase A |
|---|---|
| `ephemeral_postgres_verified` | **DESIGNED, NOT_EXECUTED** (Phase A code only) |
| `MergePilot-Test_database_verified` | `false` (NOT_PERFORMED) |
| `production_verified` | `false` (never) |

## How to run

```
python -m pytest tests/isolated_live/test_ephemeral_pg.py -v
```

No environment variables are required (and none are consulted to *execute*
anything). `EPHEMERAL_PG_VERIFY=1` is only read by `check_execution_auth()`,
which Phase A never calls to start a container.

## Phase B entry points

A Phase B executor would call, in order:
1. `check_execution_auth()` — refuse unless authorized.
2. Start the container (not yet implemented), then
   `measure_server_identity(admin_dsn)` (currently a placeholder).
3. `build_prerequisite_role_sql()` → psql.
4. `build_migration_commands(...)` → apply each of the 13 audit-db migrations.
5. `build_reader_role_sql(ephemeral_password)` → psql.
6. Apply `ISOLATED_LIVE_MIGRATIONS` (001, 002).
7. `build_seed_sql()` → psql (attempt `bind_revision()` Option A first).
8. Construct `PostgresSnapshotSource` and run the positive/negative paths.
9. `build_cleanup_commands(name, label)` → run, then verify no residue.
