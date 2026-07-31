---
name: test-runner
description: Run a test suite in isolation and return a structured PASS/FAIL/TIMEOUT/ERROR result (exit code, duration, capped stdout/stderr tails + digests, artifacts). Use on Fixer self-test and Verifier verify. Deploy-owned trust boundary; container executor is the production path.
---

# test-runner · isolated test execution

Framework-neutral core (stdlib + jsonschema) reusing the M4-A common runtime.
The caller selects a runner profile and supplies relative ``test_paths`` plus its
own restricted env values; the workspace, executor, network policy and env
allowlist are **deploy-owned** (process env) -- never request fields.

## Trust boundary (deploy-owned, not caller)

- `MERGEPILOT_TR_WORKSPACE` — clean checkout to run against (not the dev tree).
- `MERGEPILOT_TR_EXECUTOR` — `container` (default/production) | `process`
  (trusted-dev only; requires `MERGEPILOT_TR_TRUSTED_DEV=true`).
- `MERGEPILOT_TR_NETWORK_POLICY` — `denied` (default) | `allowed`. A `process`
  executor cannot enforce `denied` -> the run is refused (never lied).
- `MERGEPILOT_TR_ENV_ALLOWLIST` — admin-fixed master env allowlist.
- `MERGEPILOT_TR_IMAGE` — pinned digest; `MERGEPILOT_TR_DOCKER_TRANSPORT`
  (`native`|`wsl`|`auto`); `MERGEPILOT_TR_WSL_DISTRO` (default `Ubuntu-22.04`).
- `MERGEPILOT_TR_ARTIFACT_ROOT` — deploy-controlled artifact directory.
- `MERGEPILOT_TR_{MEMORY,CPUS,PIDS,MAX_TIMEOUT_MS,MAX_OUTPUT_BYTES}`.

## Input (business `input`)

`runner_key`, `test_paths: [relative]`, optional `env_values` (caller's own
values; intersected with the allowlist; sensitive keys stripped), `timeout_ms`,
`max_output_bytes`, `expected_profiles_version`. **No**
`command`/`argv`/`workspace_root`/`artifact_globs` fields (rejected by schema).

## Output (business `output`, schema_version `"1"`)

`verdict` (PASS|FAIL|TIMEOUT|ERROR), `exit_code`, `duration_ms`, `timed_out`,
`summary{passed,failed,skipped,errors}`, `stdout_digest`/`stderr_digest`,
capped `stdout_tail`/`stderr_tail`, `executor`, `isolation`, `network_policy`,
`resource_limits`, `artifacts[]` (always `[]` for container executor — see below),
`truncated`.

### Artifact contract (frozen M4-C v1)

- **Container executor**: `artifacts` is **always `[]`**. The container uses
  an ephemeral tmpfs (`/artifacts`, 8 MiB hard limit) which is destroyed on
  container exit. File artifacts cannot be extracted post-run. The structured
  output (verdict, summary, stdout/stderr digests) is the authoritative result.
- **Subprocess executor** (trusted-dev): `artifacts` may contain files from
  the host-side artifact directory with digests (same `MAX_ARTIFACT_*` limits).

## Runner profiles

Versioned in `config/runner-profiles.v1.json` (`profiles_version 1.0.0`),
validated by `schema/runner-profiles.schema.json`. v1 ships **only `pytest`**
(`python -m pytest`, `image_repository mergepilot/test-runner-py`,
`pytest==8.4.2`). The production image is deploy-provided as
`repository@sha256:<digest>` (no tag fallback); argv is built from the profile +
relative test_paths -- no free-form argv.

## Status / errors (generic codes; subcode rides in ``message``)

- `verdict=PASS` -> status OK, exit 0. `verdict=FAIL` -> status OK, **exit 10**
  (business failure, not a runtime error).
- `verdict=TIMEOUT` -> status ERROR, `TIMEOUT` (exit 3); structured summary kept.
- `verdict=ERROR` -> status ERROR; `DEPENDENCY_UNAVAILABLE`(5) if the executor
  could not start, else `INTERNAL_ERROR`(1). Sandbox cleanup failure is a
  safety ERROR (never best-effort-then-success).
- Pre-run: `INVALID_INPUT`(2) bad input/path-escape; `DENIED`(4) no trusted
  workspace / process-without-trusted-dev / network-denied-unenforceable.

## Isolation / safety

- Container (production): non-root `uid:gid`, `--network=none`, digest image, CPU/mem/PID
  limits, `--cap-drop=ALL --security-opt=no-new-privileges`, read-only root,
  tmpfs `/tmp` (64 MiB) + tmpfs `/artifacts` (8 MiB hard execution-time limit);
  workspace mounted read-only. Cleanup filters by run label (no global `docker ps -a`).
- Process (trusted-dev): argv with `shell=False`; process-tree kill
  (`start_new_session`+`killpg` on POSIX, `taskkill /T /F` on Windows,
  best-effort). Not a strong sandbox.
- Child env built from a minimal baseline + (allowlist ∩ caller values) minus
  sensitive keys (`*_TOKEN/SECRET/PASSWORD/PASSWD/KEY/CREDENTIAL/AUTH/COOKIE/DSN`,
  `PG_PASS/PG_PASSWORD/PG_DSN`, `MERGEPILOT_APPROVER_PASS`, all `MERGEPILOT_TR_*`);
  `os.environ` is never copied.
- Tests run in a one-shot sandbox copy (no symlink follow; `.git`/credentials
  excluded). `max_output_bytes` is the total stdout+stderr budget (default 256 KiB,
  hard 512 KiB < 1 MiB envelope); streams are drained live with rolling digests +
  bounded tails (no unbounded temp files).

## side_effects

Accurately declared: `fs_tmp` (sandbox + temp captures) and `process_exec`
(the test subprocess / container). Never empty.
