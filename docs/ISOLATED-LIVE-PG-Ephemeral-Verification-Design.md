# ISOLATED_LIVE PostgreSQL Ephemeral Verification — Design (Revised)

**Status**: Design only — not executed, not pushed, not merged
**Base**: `b2108498e7e1410a386685e987026fbfc33fd52b` (origin/main, P2 merged)
**Created**: 2026-08-13 (revised)

## 1. Audit Summary

### PostgreSQL Driver
- **Driver**: `psycopg2-binary==2.9.12` (installed on Windows Python 3.9.25)
- **Declared in**: `skills/case_retrieval/requirements.txt`, `tools/rag/requirements.txt`, `tools/policy-gateway/requirements.txt`
- **Demo Console**: uses lazy import in `postgres_source.py`; REPLAY/FILE modes need no driver
- psycopg2-binary is an optional dependency for POSTGRES_ISOLATED mode, not for REPLAY/FILE

### Docker / PostgreSQL Test Infrastructure
- **Docker image**: `pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b` (digest-pinned, same as m4f1 tests)
- **MergePilot-Test Docker**: available as root (v29.1.3); image already cached
- **Ubuntu-22.04**: Stopped (production — must not touch)
- **Windows**: no native `psql`; Docker via WSL is the current environment's chosen isolation execution method, not the only possible approach for all environments
- **pgvector**: required by the full migration chain (init.sql and m4f1 tables depend on it); NOT a direct query dependency of `PostgresSnapshotSource`

### Migration Order (authoritative, from run_schema_foundation.sh)

```
init
→ m3_state
→ m3b_policy
→ m3b_b4
→ m3b_b4c
→ m3b_b4c1
→ m3b_b4c1_1
→ m3b_b4d1
→ m3c_state
→ m4f1_state      (applied twice — idempotency verification, per m4f1)
→ m4f1_hotfix_1    (applied twice — idempotency verification, per m4f1)
→ CREATE ROLE mergepilot_reader (before 001)
→ 001_environment_identity
→ 002_mergepilot_reader_acl
```

**m4f1_state and m4f1_hotfix_1 idempotency**: Both are applied **twice** in
`run_schema_foundation.sh` to verify idempotency. The ephemeral harness MUST
replicate this two-round pattern to be consistent with the authoritative m4f1
test suite.

### mergepilot_reader Role Creation

**Order**: Must be created AFTER all audit-db migrations but BEFORE
`001_environment_identity.sql` (which grants SELECT to this role) and
`002_mergepilot_reader_acl.sql` (which checks role existence).

**Role definition**:
```sql
CREATE ROLE mergepilot_reader
    LOGIN PASSWORD '<ephemeral-random-password>'
    NOINHERIT
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION
    NOBYPASSRLS;

ALTER ROLE mergepilot_reader
    SET default_transaction_read_only = on;
```

The `SET default_transaction_read_only = on` ensures the role's sessions are
read-only by default at the PostgreSQL level, independent of the application's
own `BEGIN READ ONLY` transaction.

### Windows Execution Conditions
- Docker available only via WSL MergePilot-Test (as root)
- Python 3.9.25 on Windows has psycopg2-binary installed
- Network: WSL containers bind to localhost; Windows Python can connect via `127.0.0.1:<port>`
- `core.autocrlf` must be false for migration file byte-exactness

## 2. Verification Classification (precise definitions)

| Classification | Definition | Current Status |
|---|---|---|
| `demo_console_suite_verified` | Full `tests/demo_console/` suite: 337 discovered / 331 passed / 6 skipped / 0 failed | ✅ |
| `postgres_source_mock_verified` | FakeCursor/FakeConnection mock tests in `test_postgres_source.py` | ✅ (part of above) |
| `static_migration_verified` | Parse `.sql` files, verify column coverage; no DB connection | ✅ (part of above) |
| `ephemeral_postgres_verified` | Containerized PostgreSQL on an authorized test daemon: real migrations, real identity gate, real ACL, real read-only transaction, real column probe, real DemoBundle assembly | **DESIGNED, NOT_EXECUTED** |
| `MergePilot-Test_database_verified` | Connect to actual MergePilot-Test audit database and read real data | **false** (NOT_PERFORMED) |
| `MergePilot-Test_application_integration_verified` | Full application integration with MergePilot-Test runtime | **false** (NOT_PERFORMED) |
| `production_verified` | Production database access | **false** (never) |

**Note**: The 331 passed tests include 33 REPLAY tests, 123 ISOLATED_LIVE P1 tests
(with 1 POSIX skip), and 175 PostgresSnapshotSource tests (with 5 ephemeral
placeholder skips). Not all 331 are mock tests — 33 are REPLAY tests that test
the original Demo Console without any PostgreSQL involvement.

## 3. Deterministic Synthetic Seed Data

All data is explicitly synthetic — not real production or competition data.
Every FK is resolvable; every SHA is 40-char lowercase hex; every digest is
64-char lowercase hex. All NOT NULL / CHECK / FK constraints are satisfied.

### Run 1: Success (`run-eph-ok`)

| Table | Row | Key fields |
|---|---|---|
| `task_runs` | 1 | `run_id='run-eph-ok'`, `status='PASS'`, `repo='test/repo-alpha'`, `pr_number=42` |
| `mcp_calls` | 1 | `request_id='mcp-eph-001'`, `caller_agent='coordinator'`, `tool='create_pull_request'`, `decision='ALLOW'`, `result_status='OK'`, `git_sha='1111111111111111111111111111111111111111'` |
| `revision_bindings` | 1 | `binding_id='rb-eph-ok'`, `run_id='run-eph-ok'`, `repo='test/repo-alpha'`, `pr_number=42`, `base_sha='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'`, `head_sha='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'`, `source_call_id='mcp-eph-001'`, `source_evidence_digest='cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc'` |
| `run_pr_bindings` | 1 | `binding_id='prb-eph-ok'`, `run_id='run-eph-ok'`, `repo='test/repo-alpha'`, `pr_number=42`, `fix_branch='fix/run-eph-ok'`, `base_branch='main'`, `head_sha='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'` |
| `stage_runs` | 3 | Rows for stages: `(run_id='run-eph-ok', stage='review', agent='reviewer', attempt=1, status='COMPLETED')`, `(stage='fix', agent='fixer', ...)`, `(stage='verify', agent='verifier', verdict='PASS', ...)` |
| `stage_events` | 3 | `event_id` in `evt-eph-r1`, `evt-eph-r2`, `evt-eph-r3`; matching `run_id`, `stage`, `status='PROCESSED'` |
| `audit_events` | 5 | `task_id='run-eph-ok'`, actions: `'review','fix','verify','merge','close_pr'` |
| `rollback_runs` | 0 | (none — success run) |

Expected: `final_status='PASS'`, 3 workflow_stages, `source_commit='bbbbbbbb...'`

### Run 2: Unknown Status (`run-eph-unknown`)

| Table | Row | Key fields |
|---|---|---|
| `task_runs` | 1 | `run_id='run-eph-unknown'`, `status=NULL` (NULL tests unknown-mapping path) |
| Other tables | 0 | Minimal — only task_runs exists |

Expected: `final_status='UNKNOWN'` (never MERGED)

### Run 3: Missing Revision Binding (`run-eph-no-rev`)

| Table | Row | Key fields |
|---|---|---|
| `task_runs` | 1 | `run_id='run-eph-no-rev'`, `status='PASS'` |
| `revision_bindings` | 0 | (deliberately absent for this run_id) |
| `mcp_calls` | 0 | (no revision → no source call) |

Expected: `source_commit=null`, `provenance_status='NOT_AVAILABLE'`

### Run 4: Rollback (`run-eph-rollback`)

| Table | Row | Key fields |
|---|---|---|
| `task_runs` | 1 | `run_id='run-eph-rollback'`, `status='ROLLED_BACK'` |
| `stage_events` | 1 | `event_id='evt-eph-rb1'`, `run_id='run-eph-rollback'`, `event_type='POST_MERGE_VERIFY_FAILED'`, `status='PROCESSED'` |
| `rollback_runs` | 1 | `rollback_id='rb-eph-rb1'`, `parent_run_id='run-eph-rollback'`, `reverted_merge_sha='dddddddddddddddddddddddddddddddddddddddd'`, `repo='test/repo-alpha'`, `pr_number=42`, `trigger_event_id='evt-eph-rb1'`, `status='COMPLETED'`, `fail_reason='test_failure'` |

Expected: `final_status='ROLLED_BACK'`, `rollback_events` non-empty

### Run 5: Missing Run (`run-eph-missing`)

| Table | Row |
|---|---|
| (none) | No `task_runs` row inserted for this run_id |

Expected: `RUN_NOT_FOUND` error code from `PostgresSnapshotSource`

### environment_identity

| Table | Row |
|---|---|
| `environment_identity` | 1 row: `environment_id='mergepilot-test-ephemeral'` |

### audit_events.task_id vs run_id

`audit_events.task_id` is the P2 query's alias for `run_id` filtering. The
seed data sets `task_id` to the same value as `run_id` (e.g.,
`task_id='run-eph-ok'`). This is consistent with the controller's audit
write pattern.

## 4. Ephemeral Harness Design

### Execution Flow

```
1. Start disposable pgvector/pgvector:pg16 container
   (image: pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b)
   Container name: m6rag-eph-<timestamp>, labeled for cleanup
   Port: auto-assigned on host

2. Wait for readiness (pg_isready)

3. Apply migrations in authoritative order:
   init → m3_state → m3b_policy → m3b_b4 → m3b_b4c → m3b_b4c1
   → m3b_b4c1_1 → m3b_b4d1 → m3c_state
   → m4f1_state (round 1) → m4f1_state (round 2: idempotency)
   → m4f1_hotfix_1 (round 1) → m4f1_hotfix_1 (round 2: idempotency)

4. CREATE ROLE mergepilot_reader (LOGIN, NOINHERIT, NOSUPERUSER, ...)
   ALTER ROLE mergepilot_reader SET default_transaction_read_only = on

5. Apply 001_environment_identity.sql

6. Apply 002_mergepilot_reader_acl.sql

7. INSERT deterministic seed data (all 5 runs)

8. Measure and freeze expected server identity:
   - SELECT inet_server_addr()::text  → record actual value
   - SELECT inet_server_port()        → record actual port
   - Set application_name in DSN connection string

9. Construct PostgresSnapshotSource with reader DSN
   (using measured server address/port, expected_application_name)

10. Verify: initial_load() succeeds for run-eph-ok
    - bundle schema valid (ISOLATED_LIVE)
    - bundle_sha256 recomputable
    - final_status='PASS'
    - workflow_stages non-empty
    - RAG boundaries (adopted=false, untrusted=true)

11. Test each seed run (ok/unknown/no-rev/rollback/missing)

12. Start loopback HTTP server (port=0, OS-assigned)
    - GET /api/live/snapshot → valid JSON
    - GET /api/live/status → all boundary fields
    - POST/PUT/PATCH/DELETE → 405

13. Fail-closed negative tests (each in independent isolation — see §5)

14. Stop server, verify thread/port cleanup

15. Cleanup (see §6)

16. Verify residue
```

### Server Identity Design

- **Do NOT pre-set** `expected_server_addresses` to `['127.0.0.1']` — the
  Docker container's `inet_server_addr()` depends on the Docker network mode
- **Execute-time measurement**: after starting the container, connect as admin
  and run `SELECT inet_server_addr()::text, inet_server_port()` to get the
  actual values
- **Freeze**: use the measured values as `expected_server_addresses` and
  `expected_server_port` in the `PostgresSnapshotSource` constructor
- **Application name**: set explicitly in the DSN connection string
  (e.g., `application_name=mergepilot_isolated_live`)
- All four (database name, canonical role, environment marker, server identity)
  must match exactly — no wildcard or prefix matching

## 5. Negative Test Isolation Strategy

Each negative test modifies database state (ACL, marker, role defaults). To
avoid cross-contamination:

| Test | Isolation Method | Restore |
|---|---|---|
| Wrong database | Use a second empty database | N/A (separate DB) |
| Wrong role (superuser) | Connect as `postgres` superuser | N/A (separate connection) |
| Write-privileged role | `GRANT INSERT ON task_runs TO mergepilot_reader` | `REVOKE INSERT ON task_runs FROM mergepilot_reader` (admin connection) |
| Missing environment marker | `DELETE FROM environment_identity` | `INSERT INTO environment_identity (environment_id) VALUES ('mergepilot-test-ephemeral')` (admin connection) |
| Mismatched marker | `UPDATE environment_identity SET environment_id='wrong'` | Restore original value (admin connection) |
| Transaction read-only off | `ALTER ROLE mergepilot_reader SET default_transaction_read_only = off` | `ALTER ROLE mergepilot_reader SET default_transaction_read_only = on` (admin connection) |
| RUN_NOT_FOUND | Query nonexistent run_id | N/A (read-only, no state change) |

Each negative test runs in a `try/finally` that restores the original state
via an admin connection. The admin connection is separate from the reader
connection and uses the container's superuser credentials.

## 6. Cleanup Contract

After all tests, the harness MUST verify:

| Check | Method |
|---|---|
| HTTP server stopped | `shutdown()` + `server_close()` + thread `join(timeout=5)` + assert not alive |
| Poller thread stopped | `stop()` + `join(timeout=5)` + assert not alive |
| Container removed | `docker rm -f <exact-container-name>` — verify by name AND label |
| No matching containers | `docker ps -a --filter name=<name>` returns empty |
| No matching networks | `docker network ls --filter label=<label>` returns empty |
| Published port closed | `socket.connect((host, port))` fails |
| Temp directory deleted | `os.path.exists(tmpdir)` is False |
| No persistent Docker volume | `docker volume ls` has no new entries (or verify by label) |
| WSL distribution unchanged | `wsl -l -v` shows MergePilot-Test and Ubuntu-22.04 in same state as before |
| No DSN password in logs | Scan harness stdout/stderr for password patterns |

If any check fails, the harness reports the specific failure and exits non-zero.
The harness does NOT claim "cleanup successful" if any check fails.

## 7. Can Verify vs Cannot Verify

**Can verify (ephemeral containerized PostgreSQL)**:
- Real PostgreSQL schema compatibility (pgvector/pg16)
- Real migration application order and two-round idempotency
- Real ACL/role enforcement (SELECT granted, writes revoked)
- Real `default_transaction_read_only` enforcement
- Real identity gate (database/user/read-only/server/marker/catalog)
- Real read-only transaction (REPEATABLE READ READ ONLY)
- Real column-level catalog probe
- Real DemoBundle assembly from live SQL queries
- Real HTTP server serving live snapshot/status
- Real fail-closed on all negative paths
- Real cleanup (no residue)

**Cannot verify (without real MergePilot-Test)**:
- Production data volume, diversity, or historical patterns
- Real MergePilot-Test audit schema (may have additional objects)
- PolarDB-PG compatibility (if different from open-source pgvector/pg16)
- Network latency or concurrent multi-connection access
- Real GitHub MCP integration
- Real OTel trace integration
- Production HiClaw or SLS

## 8. Phased Implementation Plan

### Phase A: Harness Code (no execution)
- `tests/isolated_live/ephemeral_harness.py` — container lifecycle, migration applier, seeder, cleanup
- `tests/isolated_live/test_ephemeral_pg.py` — test cases (gated on env var)
- Reuse patterns from `tests/m4f1/run_schema_foundation.sh` and `tests/rag/pgvector_isolated_verify.py`
- Docker interaction via subprocess (not Python Docker SDK — keeps stdlib-only for non-PostgreSQL paths)

### Phase B: Execution (requires MergePilot-Test Docker)
- Start MergePilot-Test WSL (Ubuntu-22.04 stays Stopped)
- Run ephemeral harness via WSL or Windows Python (connecting to container's published port)
- Verify all positive and negative test paths
- Record results

### Phase C: Evidence (only after B passes)
- Candidate path: `evidence/post-m7/isolated-live/ephemeral-pg-verification.json`
- `post_m7_capability_extension = true`
- Not part of m7-closed baseline
- Bind to execution commit

## 9. Dependencies and Security

### Dependencies
- `psycopg2-binary==2.9.12` (already installed on Windows)
- Docker daemon (MergePilot-Test, root access via `wsl -u root`)
- `pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b` (cached)

### Security
- **DSN password**: ephemeral, random per run, never logged, never committed
- **Container isolation**: labeled with unique tag, cleaned in EXIT trap
- **No production access**: Ubuntu-22.04 stays Stopped throughout
- **Port binding**: auto-assigned, not exposed to LAN
- **No persistent volumes**: ephemeral container only

## 10. Boundaries

- Docker/PostgreSQL execution = **NOT_PERFORMED** (design only)
- This design does NOT verify MergePilot-Test integration
- This design does NOT access production data
- This design does NOT create production roles or migrations
- The ephemeral harness is NOT a production tool
- Results from ephemeral tests do NOT extrapolate to production PolarDB-PG
- Dynamic 8-page UI refresh remains NOT_IMPLEMENTED
- M8 remains undefined
