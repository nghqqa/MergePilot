# ISOLATED_LIVE PostgreSQL Ephemeral Verification — Phase B (Execution)

## Status (current HEAD real-verified; execution date 2026-08-14)

> **The current tree's real ephemeral PostgreSQL execution has PASSED.** The
> HEAD was re-run twice against a live disposable pgvector container on the
> authorized MergePilot-Test daemon (execution date 2026-08-14), with cleanup
> and residue audit passing both times. Ubuntu-22.04 remained Stopped
> throughout; MergePilot-Test was restored to Stopped after execution.
>
> | Field | Value |
> |---|---|
> | `current_HEAD_ephemeral_postgres_verified` | **true** |
> | `current_HEAD_real_postgres_reexecution` | **PERFORMED** (2026-08-14, two runs) |
> | `historical_pre_amend_ephemeral_execution_passed` | **true** (pre-amendment run also passed) |
> | MergePilot-Test daemon | authorized, local unix socket endpoint |
> | real PostgreSQL re-execution verified by | TWO live runs + demo_console regression + residue audit |

Phase B implements the real executor against a one-shot disposable pgvector
container on the authorized MergePilot-Test WSL daemon. It flips the Phase A
placeholders into real tests that exercise the live PostgreSQL read path, the
identity gate, the reader ACL, the fail-closed negative matrix, and the HTTP
live server — all against synthetic seed data, never against the real
MergePilot-Test application database or any production database.

## What Phase B verifies (current HEAD real-verified, 2026-08-14)

The current HEAD was re-run against a live disposable container and passed:

| Classification | Status (current HEAD, real-verified 2026-08-14) |
|---|---|
| `ephemeral_postgres_verified` | **true** — real DB tests pass |
| `migration_execution_verified` | **true** — 15 migration applications + 2 role bootstraps = 17 ops |
| `reader_acl_verified` | **true** — SELECT on 9 tables, writes denied, read-only gate |
| `http_live_path_verified` | **true** — real reader → LivePoller → HTTP; snapshot/status/405 |
| `cleanup_verified` | **true** — addClassCleanup + external residue audit pass |
| `ephemeral_bind_revision_contract_verified` | **true** — Option A `bind_revision()` succeeded |
| `MergePilot-Test_ephemeral_docker_verified` | **true** — disposable Docker PostgreSQL on MergePilot-Test |

> `MergePilot-Test_ephemeral_docker_verified = true` means ONLY that a one-shot
> Docker PostgreSQL on the MergePilot-Test daemon passed. It does NOT mean the
> existing MergePilot-Test application database was verified.

## What Phase B does NOT verify (NOT_VERIFIED / false)

| Classification | Status |
|---|---|
| `revision_producer_contract` | **NOT_VERIFIED** — Option A only verifies the narrow `bind_revision()` call, not the producer contract |
| `audit_producer_contract` | **NOT_VERIFIED** — `audit_events` are synthetic/admin seed; the controller write path is never invoked |
| `MergePilot-Test_database_verified` | **false** — the real application database was not accessed |
| `MergePilot-Test_application_integration_verified` | **false** — no application integration |
| `production_verified` | **false** — never |
| M8 | **remains undefined** |

## Boundary honesty

- The ephemeral container results do NOT extrapolate to production PolarDB-PG.
- Option A (`bind_revision()`) succeeds, but this is the narrow producer-side
  function call verification only — recorded as
  `ephemeral_bind_revision_contract_verified = true`. It is NOT a claim that
  `revision_producer_contract = VERIFIED`.
- `audit_events` are inserted by the synthetic seed (admin path). The
  controller's audit-event write path is never invoked, so
  `audit_producer_contract = NOT_VERIFIED`.

## Execution contract

### Authorization gate
`EPHEMERAL_PG_VERIFY=1` (exact `"1"`) AND the MergePilot-Test daemon reachable
AND Docker endpoint = `unix:///var/run/docker.sock` (no TCP/SSH/remote) AND the
digest-pinned image cached. MergePilot-Test must be Running (never implicitly
started by the harness); Ubuntu-22.04 must remain Stopped/untouched.

### Image
`pgvector/pgvector@sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b`
(digest-pinned; the `pg16` tag is informational only). The container is started
with the digest directly — NO floating-tag fallback. The approved local Image ID
is resolved pre-start via `docker image inspect IMAGE_DIGEST` and must match the
`authorization_context.image_id`; post-start the running container's `.Image`
must equal it AND RepoDigests must contain the approved digest. `--pull=never`,
`--restart=no`.

### Password transport (no argv leak)
Admin password delivered via `docker run --env-file /dev/stdin` with env bytes
piped through `subprocess.run(input=...)` (POSTGRES_USER=mergepilot,
POSTGRES_PASSWORD, POSTGRES_DB). Reader password travels only inside `psql`
SQL piped over stdin (`CREATE ROLE`/`ALTER ROLE`). Every argv is checked by
`_assert_argv_safe` before execution; collected stdout/stderr is passed through
`redact_secrets()` immediately on collection.

### Network
Container binds ONLY `-p 127.0.0.1::5432` (IPv4 loopback, auto-assigned host
port). `host_address`/`host_port` (Windows psycopg2 DSN) are strictly separated
from `server_address`/`server_port` (measured via real TCP
`inet_server_addr()`/`inet_server_port()`, used as `expected_server_*`).
`expected_server_port` = 5432 (container port), NOT the random host port.

### Bootstrap sequence (17 operations)
1. Phase 0: prerequisite roles (`policy_gateway_l2`, `mergepilot_approver`)
2. Phase 1: 13 audit-db migrations (m4f1_state ×2, m4f1_hotfix_1 ×2 for idempotency)
3. Phase 2: `mergepilot_reader` role (all privileged attrs OFF, read-only default)
4. Phase 3: 2 ISOLATED_LIVE migrations (001, 002)

Then seed part 1 (task_runs/run_pr_bindings/mcp_calls) → Option A
`bind_revision()` → (fallback Option B if A fails) → seed part 2
(stage_runs/stage_events/audit_events/rollback/missing).

### Migration file integrity (Fix 3)
Every migration SQL file is integrity-verified by the executor BEFORE any SQL
runs: the working-tree content's git blob hash is compared to
`PHASE_B_BASE_COMMIT` (7c5630a6f2f6c5049f028312caf895cf8cd2cbc9). Symlinks,
directories, path-escape, and glob auto-discovery are all rejected. On any
mismatch → `MIGRATION_INTEGRITY_MISMATCH` and NO SQL is applied.

Migration counts (authoritative):
- audit-db applications = 13; audit-db distinct files = 11
- ISOLATED_LIVE applications = 2; ISOLATED_LIVE distinct files = 2
- total applications = 15; total distinct files = 13
- executor bootstrap operations = 17 (15 migrations + 2 role bootstraps)

### Cleanup (Fix 4)
`cleanup_and_verify` is idempotent, tolerates partial startup. Before removal
it verifies the container's ID + name + session label ALL match this session
(`RESOURCE_OWNERSHIP_MISMATCH` otherwise → no removal, never deletes a resource
that does not belong to this session). Removal uses the container ID
(`docker rm -fv <id>`); anonymous volumes are recorded pre-removal and verified
absent after. `check=False` commands have their returncode explicitly handled
(a failed command is a cleanup failure, not "resource absent"). The
authoritative `cleanup_verified` gate is an external read-only residue audit run
after the test process exits.

### Authorization gate (Fix 2)
`check_execution_auth` verifies, in order, fail-closed at the first miss (no
Docker command runs if an earlier gate fails): EPHEMERAL_PG_VERIFY=="1";
MergePilot-Test present + initially Running (a Stopped distro is NOT implicitly
started); Ubuntu-22.04 state recorded but never invoked; docker endpoint ==
unix:///var/run/docker.sock (no TCP/SSH/remote); DOCKER_HOST empty or local
unix socket; daemon fingerprint complete (Server ID/Name/Root Dir/Version);
IMAGE_DIGEST cached. The fingerprint is returned for pre/post-execution compare.

### Image (Fix 1)
The container is started with the digest-pinned `IMAGE_DIGEST` directly — NO
floating-tag fallback. The approved local Image ID is resolved pre-start via
`docker image inspect IMAGE_DIGEST`; post-start the running container's
`.Image` must equal it AND the image's RepoDigests must contain `IMAGE_DIGEST`.
A digest-run failure is BLOCKED (no tag retry).

### Combined error (Fix 6)
`start_and_prepare` wraps start+prepare: on primary failure, cleanup runs in a
finally; if cleanup ALSO fails, `EphemeralExecutionAndCleanupError` carries
BOTH stable, redacted codes (primary_error_code + cleanup_error_code). Neither
swallows the other.

### Structured seed split (Fix 7)
`build_seed_sql_parts()` returns (before_bind_sql, option_b_revision_sql,
after_bind_sql) structurally — no text parsing. Option A `bind_revision()`
runs between before and after; the Option B fallback row is applied only if
Option A fails. `build_seed_sql()` returns their concatenation (byte-identical
to the historical monolithic seed).

## Test results

**Real execution against current HEAD (2026-08-14, two live runs):**
```
Run 1: EPHEMERAL_PG_VERIFY=1 ... isolated_live
  discovered = 168, passed = 168, skipped = 0, failed = 0, errors = 0
  (TestEphemeralLive executed 7 real tests — no NOT_EXECUTED skip)
Run 2: EPHEMERAL_PG_VERIFY=1 ... isolated_live
  discovered = 168, passed = 168, skipped = 0, failed = 0, errors = 0
demo_console regression (EPHEMERAL_PG_VERIFY unset):
  discovered = 337, passed = 331, skipped = 6, failed = 0, errors = 0
```

`passed = discovered - skipped - failed - errors`. **Skipped is NEVER counted as
passed.** Both real runs used `-W error::ResourceWarning` (0 warnings). The
external Docker residue audit after each run found no `m6rag-eph` containers,
networks, or volumes.

**Unauthorized path (EPHEMERAL_PG_VERIFY unset, no Docker):**
```
Unauthorized path (class-level skip semantics):
- loader_discovered_test_methods = 168
- runner_reported_testsRun = 161
- non_live_test_methods_passed = 161
- class_level_skip_events = 1
- TestEphemeralLive methods NOT_EXECUTED = 7
- failed = 0
- errors = 0
```
`TestEphemeralLive.setUpClass()` raises `unittest.SkipTest` when unauthorized.
This produces a SINGLE class-level skip event; the 7 live test methods do NOT
enter `testsRun`. Therefore the runner reports `Ran 161 ... OK (skipped=1)`,
but this must NOT be converted to `passed=160`. The formula
`passed = discovered - skipped - failed - errors` applies only to ordinary
per-test counting and does NOT apply to this class-level SkipTest case.

### Real-execution measurements (current HEAD)
- **Migration / bootstrap:** 13 audit-db applications + 2 ISOLATED_LIVE
  applications = 15 migration-file applications (13 distinct files); plus
  Phase 0 prerequisite roles + Phase 2 reader role = **17 bootstrap operations**.
- **Option A `bind_revision()`:** **succeeded** →
  `ephemeral_bind_revision_contract_verified = true` (NOT a claim that
  `revision_producer_contract = VERIFIED`).
- **Negative matrix:** **10 negative scenarios** asserting **7 distinct stable
  error codes**, each modify→fail→restore→fresh-reader-LIVE:
  RUN_NOT_FOUND ×1, WRONG_DATABASE ×1, WRONG_ROLE ×2,
  ENVIRONMENT_ID_NOT_VERIFIED ×1, ENVIRONMENT_ID_MISMATCH ×1, NOT_READ_ONLY ×1,
  WRONG_SERVER ×3. (Scenario count and distinct error-code count are reported
  separately and must not be conflated.)
- **Container:** digest-pinned startup (`--pull=never`, no tag fallback);
  `127.0.0.1` IPv4-loopback auto port; host_port ≠ server_port (5432).
- **Cleanup:** ownership triple-match (ID/name/label) removal; anonymous
  volumes recorded + verified absent; host port closed; no secret/temp residue.

### Mock / structural test coverage (all passing)

In addition to the live `TestEphemeralLive` run, the following Mock/structural
classes cover the hardened executor without contacting Docker/WSL/PostgreSQL:

- **`TestAuthContextStrict` (9 tests)** — authorization_context completeness:
  the success result must explicitly carry `endpoint`, `docker_host`,
  `image_digest`, and `image_id`; the executor constructor deep-copies the
  context (no caller-mutable reference, no caller-dict mutation); strict
  validation rejects any missing/wrong `endpoint` / `image_digest` / `image_id`
  / `docker_host` with `AUTH_CONTEXT_INVALID` before any Docker command;
  inference/self-rewrite of `image_digest` is forbidden.
- **`TestEnvironmentRecheck` (9 tests)** — post-cleanup environment recheck
  ordering and the `_environment_touched` gate: a pre-Docker
  `AUTH_CONTEXT_INVALID` never triggers a recheck; when
  `_environment_touched` is false the recheck is a no-op; the recheck runs
  `wsl -l -v` first and, if MergePilot-Test is missing or Stopped, raises
  `ENVIRONMENT_FINGERPRINT_CHANGED` WITHOUT issuing any `wsl -d` / Docker
  command (no implicit distro start); any change to Ubuntu state / DOCKER_HOST
  / endpoint / fingerprint fields / image_id is detected; an all-match recheck
  passes.
- **`TestCleanupLifecycle` (8 tests)** — HTTP / poller / reader-source cleanup
  error handling: failures produce stable codes
  (`HTTP_SHUTDOWN_FAILED` / `POLLER_STOP_FAILED` / `POLLER_STILL_ALIVE` /
  `SOURCE_CLOSE_FAILED`), the failed object reference is RETAINED for retry,
  `_cleaned` stays false, and a second cleanup attempt retries and succeeds;
  cleanup error messages contain no secret.
- **`TestExecutorStructural`** — digest-pinned startup argv (no tag fallback),
  migration-integrity enforcement (validate-all-before-execute; repeated
  migrations verified once / applied twice), cleanup resource ownership
  (ID/name/label triple), `EphemeralExecutionAndCleanupError` combined codes,
  structured seed-split parts.
- **`TestWslDistroParse`** — robust `wsl -l -v` parsing (leading `*`, UTF-16
  NUL, names with spaces, header-agnostic, malformed-ignore, fail-closed).
- **`TestDockerHostAllowlist`** — DOCKER_HOST allowlist (only empty or the
  exact approved unix socket; rejects other sockets / tcp / ssh / npipe).
- **Hardened `TestExecutionGate`** — full auth-gate Mock coverage (distro
  Stopped / remote endpoint / DOCKER_HOST TCP / image not cached /
  fingerprint incomplete / unauthorized-path-never-touches-Docker).

## Negative matrix coverage (Fix 5)
The `test_fail_closed_negative_paths` real test asserts these stable error
codes (each modify→fail→restore→fresh-reader-LIVE): RUN_NOT_FOUND,
WRONG_SERVER (address / port / application_name), WRONG_ROLE (write-privilege
grant + non-reader current_user), WRONG_DATABASE, NOT_READ_ONLY,
ENVIRONMENT_ID_NOT_VERIFIED (0 marker rows), ENVIRONMENT_ID_MISMATCH. These
are implemented but NOT re-run after this amendment.

## Files added/modified

- `tests/isolated_live/ephemeral_executor.py` — the real Phase B executor
  (hardened: digest startup, migration integrity, cleanup ownership, combined
  error, structured seed, authorization_context deep-copy + strict validation,
  `_environment_touched` recheck gate, HTTP/poller/source retry semantics).
- `tests/isolated_live/test_ephemeral_pg.py` — `TestEphemeralLive` (7 real
  tests, full negative matrix) + `TestExecutorStructural` + `TestAuthContextStrict`
  + `TestEnvironmentRecheck` + `TestCleanupLifecycle` + `TestWslDistroParse`
  + `TestDockerHostAllowlist` + hardened `TestExecutionGate`.
- `tests/isolated_live/ephemeral_harness.py` — corrected `IMAGE_DIGEST`
  (63→64 hex), hardened `check_execution_auth` (endpoint/DOCKER_HOST/fingerprint/
  image_id, complete non-inferable success result), `build_seed_sql_parts()`.
- `docs/ISOLATED-LIVE-PG-Ephemeral-Verification-PhaseB.md` — this document.
