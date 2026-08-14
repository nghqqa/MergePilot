# ISOLATED_LIVE PostgreSQL Ephemeral Verification — Design (Final Revised)

**Status**: Design only — not executed, not pushed, not merged
**Base**: `b2108498e7e1410a386685e987026fbfc33fd52b` (origin/main, P2 merged)
**Created**: 2026-08-13 (final revised)

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

### Complete Bootstrap and Migration Order

The full execution sequence is:

**Phase 0: Prerequisite roles** (before any audit-db migration, per `run_schema_foundation.sh:43`):
```sql
CREATE ROLE policy_gateway_l2 NOLOGIN;
CREATE ROLE mergepilot_approver NOLOGIN;
```

**Phase 1: Audit-db migration chain** (13 migration applications, 11 distinct
files; m4f1_state and m4f1_hotfix_1 each applied twice for idempotency):
```
 1. init
 2. m3_state
 3. m3b_policy
 4. m3b_b4
 5. m3b_b4c
 6. m3b_b4c1
 7. m3b_b4c1_1
 8. m3b_b4d1
 9. m3c_state
10. m4f1_state          (round 1)
11. m4f1_state          (round 2 — idempotency verification, per m4f1)
12. m4f1_hotfix_1        (round 1)
13. m4f1_hotfix_1        (round 2 — idempotency verification, per m4f1)
```

**Phase 2: ISOLATED_LIVE viewer role bootstrap**:
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

**Phase 3: ISOLATED_LIVE migrations** (2 migration applications):
```
 1. 001_environment_identity    (GRANT SELECT TO mergepilot_reader)
 2. 002_mergepilot_reader_acl   (GRANT SELECT on 9 tables; REVOKE writes)
```

**Total**: 13 audit-db + 2 ISOLATED_LIVE = 15 migration-file applications
(11 distinct audit-db files), across 2 prerequisite-role steps + 1
viewer-role bootstrap.

**m4f1_state and m4f1_hotfix_1 idempotency**: Both are applied **twice** in
`run_schema_foundation.sh` to verify idempotency. The ephemeral harness MUST
replicate this two-round pattern to be consistent with the authoritative m4f1
test suite.

### Windows Execution Conditions
- Docker available only via WSL MergePilot-Test (as root)
- Python 3.9.25 on Windows has psycopg2-binary installed
- Network: WSL containers bind to localhost; Windows Python can connect via `127.0.0.1:<port>`
- `core.autocrlf` must be false for migration file byte-exactness

## 2. Verification Classification (precise definitions)

| Classification | Definition | Status |
|---|---|---|
| `demo_console_suite_verified` | Full `tests/demo_console/` suite | ✅ 337 discovered / 331 passed / 6 skipped / 0 failed |
| `postgres_source_suite_verified` | All tests in `test_postgres_source.py` | ✅ 180 discovered / 175 passed / 5 skipped |
| `postgres_source_mock_verified` | FakeCursor/FakeConnection mock tests (subset of above) | ✅ (majority of 175) |
| `static_migration_contract_verified` | Parse `.sql` files, verify column coverage (subset of above) | ✅ (TestMigrationContractFiles) |
| `isolated_live_p1_verified` | P1 ISOLATED_LIVE tests in `test_isolated_live.py` | ✅ 124 discovered / 123 passed / 1 skipped |
| `ephemeral_postgres_verified` | Containerized PostgreSQL on an authorized test daemon | **DESIGNED, NOT_EXECUTED** |
| `MergePilot-Test_database_verified` | Connect to actual MergePilot-Test audit database | **false** (NOT_PERFORMED) |
| `MergePilot-Test_application_integration_verified` | Full application integration with MergePilot-Test runtime | **false** (NOT_PERFORMED) |
| `production_verified` | Production database access | **false** (never) |

**Test breakdown** (mutually exclusive per-file):
- `test_demo_console.py`: 33 discovered, 33 passed, 0 skipped, 0 failed
- `test_isolated_live.py`: 124 discovered, 123 passed, 1 skipped (POSIX-only), 0 failed
- `test_postgres_source.py`: 180 discovered, 175 passed, 5 skipped (ephemeral NOT_EXECUTED placeholders), 0 failed
- **Total**: 337 discovered, 331 passed, 6 skipped, 0 failed

The 175 passed `test_postgres_source.py` tests are NOT all mock tests — they
include static migration-contract tests (`TestMigrationContractFiles`) and
runtime catalog mock tests that parse actual `.sql` files. The mock/static/
ephemeral split is mutually exclusive within the 180 discovered.

## 3. Deterministic Synthetic Seed Data

All data is explicitly synthetic — not real production or competition data.
Every FK is resolvable; every SHA is 40-char lowercase hex; every digest is
64-char lowercase hex. All NOT NULL / CHECK / FK constraints are satisfied.

### Run 1: Success (`run-eph-ok`)

| Table | Row | Key fields |
|---|---|---|
| `task_runs` | 1 | `run_id='run-eph-ok'`, `status='PASS'`, `repo='test/repo-alpha'`, `pr_number=42`, `skill_data_state='ACTIVE'` (required by `bind_revision`) |
| `run_pr_bindings` | 1 | `binding_id='prb-eph-ok'`, `run_id='run-eph-ok'`, `repo='test/repo-alpha'`, `pr_number=42`, `fix_branch='fix/run-eph-ok'`, `base_branch='main'`, `head_sha='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'` |
| `mcp_calls` | 1 | `request_id='mcp-eph-001'`, `correlation_id='corr-eph-001'`, `phase='RESULT'`, `caller_agent='coordinator'`, `tool='create_pull_request'`, `decision='ALLOW'`, `result_status='OK'`, `run_id='run-eph-ok'`, `target_repo='test/repo-alpha'`, `git_sha='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'` (= base_sha, required by bind_revision provenance check), `error=NULL` |
| `revision_bindings` | via `bind_revision()` or direct-admin seed (see §3.1 below) | `binding_id` auto-generated; `run_id='run-eph-ok'`, `repo='test/repo-alpha'`, `pr_number=42`, `base_sha='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'`, `head_sha='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'`, `source_call_id='mcp-eph-001'`, `source_evidence_digest` = computed per bind_revision algorithm |
| `stage_runs` | 3 | `(run_id='run-eph-ok', stage='review', agent='reviewer', attempt=1, status='COMPLETED')`, `(stage='fix', agent='fixer', attempt=1, status='COMPLETED')`, `(stage='verify', agent='verifier', attempt=1, status='COMPLETED', verdict='PASS')` |
| `stage_events` | 3 (see §3.2 for complete fields) | Matching run_id, stage, status='PROCESSED' |
| `audit_events` | 5 | `task_id='run-eph-ok'`, actions: `'review','fix','verify','merge','close_pr'` |
| `rollback_runs` | 0 | (none — success run) |

Expected: `final_status='PASS'`, 3 workflow_stages, `source_commit='bbbbbbbb...'`

#### 3.1 Revision Provenance Strategy

**Option A (preferred): Use `bind_revision()` function** — calls the
producer-side SQL function, which internally validates:
- `task_runs.skill_data_state = 'ACTIVE'`
- `run_pr_bindings` repo/pr/head_sha match
- `mcp_calls` phase='RESULT', decision='ALLOW', result_status='OK',
  run_id matches, target_repo matches, git_sha matches base_sha
- Recomputes `source_evidence_digest` using the canonical algorithm:
  `digest(canon(source_call_id || correlation_id || tool || target_repo || run_id || git_sha || result_status), 'sha256')`

**Option B (fallback): Direct-admin INSERT** — if `bind_revision()` cannot be
called from the harness (e.g., permission issues with SECURITY DEFINER), the
harness inserts directly into `revision_bindings` as admin. In this case:

> **revision_producer_contract = NOT_VERIFIED.**
> The direct-admin seed verifies only the P2 consumer/read path
> (PostgresSnapshotSource reading the revision_bindings row). The revision
> producer contract (bind_revision function behavior) is NOT tested.

The harness SHOULD attempt Option A first. If it fails, fall back to Option B
and explicitly record `revision_producer_contract=NOT_VERIFIED`.

#### 3.2 stage_events Complete Fields

**Success run events**:
```
event_id='evt-eph-ok-r1', run_id='run-eph-ok', room_id='room-eph-ok',
  event_type='M4F_REVIEW_DISPATCH', stage='review', status='PROCESSED',
  sender='controller', body_sha256=NULL, raw_body=NULL, error=NULL,
  received_at=now(), processed_at=now()

event_id='evt-eph-ok-r2', run_id='run-eph-ok', room_id='room-eph-ok',
  event_type='M4F_FIX_DISPATCH', stage='fix', status='PROCESSED',
  sender='controller', body_sha256=NULL, raw_body=NULL, error=NULL,
  received_at=now(), processed_at=now()

event_id='evt-eph-ok-r3', run_id='run-eph-ok', room_id='room-eph-ok',
  event_type='M4F_VERIFY_DISPATCH', stage='verify', status='PROCESSED',
  sender='controller', body_sha256=NULL, raw_body=NULL, error=NULL,
  received_at=now(), processed_at=now()
```

Required NOT NULL fields per DDL: `event_id`, `room_id`, `event_type`, `status`.
`run_id` is nullable but set for P2 query compatibility.

**Rollback run events**:
```
event_id='evt-eph-rb1', run_id='run-eph-rollback', room_id='room-eph-rb',
  event_type='POST_MERGE_VERIFY_FAILED', stage='verify', status='PROCESSED',
  sender='verifier', body_sha256=NULL, raw_body=NULL, error='test failure',
  received_at=now(), processed_at=now()
```

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
| `stage_events` | 1 | See §3.2 rollback event above |
| `rollback_runs` | 1 | `rollback_id='rb-eph-rb1'`, `parent_run_id='run-eph-rollback'`, `reverted_merge_sha='dddddddddddddddddddddddddddddddddddddddd'`, `repo='test/repo-alpha'`, `pr_number=42`, `trigger_event_id='evt-eph-rb1'`, `status='REVERTED'`, `fail_reason='test_failure'` |

`rollback_runs.status` must be a valid CHECK constraint value:
`'PENDING','CONFLICT','UNSUPPORTED','REVERT_PR_OPEN','AWAITING_APPROVAL',
'REVERTING','REVERTED','REVERIFYING','RECOVERED','HELD'`.
The seed uses **`'REVERTED'`** (not `'COMPLETED'` which is not in the allowlist).

`task_runs.status='ROLLED_BACK'` is a valid `task_runs` CHECK constraint value
(`'SUBMITTED','RUNNING','PASS','FAIL','HOLD','MERGED','ROLLED_BACK'`).

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

3. Phase 0: Create prerequisite roles
   CREATE ROLE policy_gateway_l2 NOLOGIN;
   CREATE ROLE mergepilot_approver NOLOGIN;

4. Phase 1: Apply audit-db migrations (13 applications, 11 distinct files)
   init → m3_state → m3b_policy → m3b_b4 → m3b_b4c → m3b_b4c1
   → m3b_b4c1_1 → m3b_b4d1 → m3c_state
   → m4f1_state (×2) → m4f1_hotfix_1 (×2)

5. Phase 2: Create mergepilot_reader
   CREATE ROLE mergepilot_reader LOGIN ... NOINHERIT NOSUPERUSER ...
   ALTER ROLE mergepilot_reader SET default_transaction_read_only = on;

6. Phase 3: Apply ISOLATED_LIVE migrations
   001_environment_identity → 002_mergepilot_reader_acl

7. INSERT deterministic seed data (all 5 runs)

8. Optionally call bind_revision() for run-eph-ok
   (or direct-admin INSERT; record which path was used)

9. Measure and freeze expected server identity:
   - SELECT inet_server_addr()::text → record actual value
   - SELECT inet_server_port() → record actual port
   - Set application_name in DSN connection string

10. Construct PostgresSnapshotSource with reader DSN
    (using measured server address/port, expected_application_name)

11. Verify: initial_load() succeeds for run-eph-ok
    - bundle schema valid (ISOLATED_LIVE)
    - bundle_sha256 recomputable
    - final_status='PASS'
    - workflow_stages non-empty
    - RAG boundaries (adopted=false, untrusted=true)

12. Test each seed run (ok/unknown/no-rev/rollback/missing)

13. Start loopback HTTP server (port=0, OS-assigned)
    - GET /api/live/snapshot → valid JSON
    - GET /api/live/status → all boundary fields
    - POST/PUT/PATCH/DELETE → 405

14. Fail-closed negative tests (each in independent isolation — see §5)

15. Stop server, verify thread/port cleanup

16. Cleanup (see §6)

17. Verify residue
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
avoid cross-contamination, each test follows a **modify → verify-fail →
restore → verify-restored** cycle:

| Test | Modify | Verify Fail | Restore (admin) | Verify Restored (fresh reader) |
|---|---|---|---|---|
| Wrong database | Use second empty DB | WRONG_DATABASE | N/A (separate DB) | N/A |
| Wrong role (superuser) | Connect as postgres | WRONG_ROLE (superuser) | N/A | N/A |
| Write-privileged role | `GRANT INSERT ON task_runs TO mergepilot_reader` | WRONG_ROLE | `REVOKE INSERT ON task_runs FROM mergepilot_reader` | Fresh reader connection → succeeds |
| Missing marker | `DELETE FROM environment_identity` | ENVIRONMENT_ID_NOT_VERIFIED | `INSERT INTO environment_identity ...` | Fresh reader → LIVE |
| Mismatched marker | `UPDATE environment_identity SET environment_id='wrong'` | ENVIRONMENT_ID_MISMATCH | Restore original value | Fresh reader → LIVE |
| Transaction read-only off | `ALTER ROLE ... SET default_transaction_read_only=off` | NOT_READ_ONLY | `ALTER ROLE ... SET default_transaction_read_only=on` | Fresh reader → LIVE |
| RUN_NOT_FOUND | Query nonexistent run_id | RUN_NOT_FOUND | N/A (read-only) | N/A |

### Restore Verification Gate

After each restore, the harness opens a **fresh reader connection** and
verifies that a normal `initial_load()` succeeds (state=LIVE). This proves
the restore was effective.

**If restore fails**: the harness MUST fail the entire suite. The
`finally` block that performs restore is NOT permitted to swallow
exceptions — any restore/cleanup exception propagates as a test failure.

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
