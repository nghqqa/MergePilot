# M4-C: sast-scan + test-runner release evidence

This evidence set describes the reviewed M4-C delivery as of 2026-07-31. The
delivery is not committed, tagged, or pushed yet.

## Scope

- `skills/sast_scan/`: deterministic secret, Python AST, and offline dependency
  advisory engines with versioned rules and Draft 2020-12 schemas.
- `skills/test_runner/`: deploy-controlled test execution with production
  container isolation and trusted-dev subprocess support.
- `tests/m4c/`: 87 deterministic tests and the release gate runner.
- `evidence/m4/m4c/`: two pytest runs, structured container E2E evidence, and
  structured image-build evidence, and the consolidated verification record.

M3, M4-A, M4-B, `skills/common/**`, the legacy `skills/sast-scan/`, `tools/**`,
and `config/souls/**` remain unchanged.

## SASTScan contract

- All three v1 engines run; callers cannot downgrade engine coverage.
- Frozen request limits cannot be raised: 256 files, 256 KiB per file, 2 MiB
  total input, and 500 findings.
- Secret material is not included in output, messages, fingerprints, evidence
  digests, or the input digest. Digests use deterministic redacted material.
- Python syntax errors and optional engine degradation produce `PARTIAL`; rules
  version, duplicate identifiers, invalid regexes, and malformed advisories fail
  closed.
- The dependency engine is an offline advisory matcher and only treats exact
  `==` pins as version matches. It is not a real-time vulnerability service.

## TestRunner contract

- Workspace, executor, network policy, environment allowlist, artifact root,
  image digest, transport, UID/GID, and resource limits are deploy-owned
  `MERGEPILOT_TR_*` settings. Request input cannot provide command, argv,
  workspace, or artifact glob fields.
- Production execution uses an argv-only container invocation with non-root
  UID:GID, `--network=none`, read-only root, CPU/memory/PID limits,
  `--cap-drop=ALL`, and `no-new-privileges`.
- Environment inheritance is deny-by-default and removes credential-shaped
  keys even when allowlisted.
- Cleanup is fail-closed. Each container has a unique name and run label;
  cleanup checks `docker rm -f` and queries only that run label for residue.
- Exit classification separates business FAIL from runner ERROR. Business FAIL
  returns envelope status `OK` and CLI exit 10; runtime failures use the common
  structured error codes.

### Artifact contract (frozen M4-C v1)

- Container executor: `artifacts` is always `[]`. `/artifacts` is an in-container
  tmpfs with `size=8388608`, so untrusted writes have an execution-time 8 MiB
  limit. The tmpfs is destroyed when the container stops.
- Subprocess executor (`trusted-dev` only): host-side artifacts may be returned
  with relative paths and SHA-256 digests. File count, total size, path,
  symlink, socket, and device checks fail closed.
- The output schema enforces `executor=container -> artifacts.maxItems=0`, and
  core only collects host artifacts for `executor=process`.

This design does not create a Docker volume or host bind mount for container
artifacts. It does not claim that tmpfs can never use host swap.

## Container E2E

Four real production-chain scenarios ran through:

`skills.test_runner.run -> core.run -> container_executor.run -> docker run -> cleanup`

| Scenario | Envelope status | Verdict | CLI rc | Artifacts | Residual containers |
|---|---|---|---:|---:|---:|
| pass | OK | PASS | 0 | 0 | 0 |
| timeout | ERROR / TIMEOUT | TIMEOUT | 3 | 0 | 0 |
| error | ERROR / INTERNAL_ERROR | ERROR | 1 | 0 | 0 |
| tmpfs_quota | OK | PASS | 0 | 0 | 0 |

The quota fixture writes 9 MiB to the 8 MiB tmpfs, requires
`OSError.errno == ENOSPC`, prints `TMPFS_QUOTA_ERRNO=28`, and the E2E runner
records `quota_errno: 28` in `container-e2e.json`. A PASS therefore proves the
expected quota error was observed rather than an arbitrary I/O failure.

E2E environment:

- WSL2 distro: `Ubuntu-22.04`
- Docker client/server: `29.5.2`
- Image: `localhost:5000/mergepilot/test-runner-py@sha256:41c6ab6e8dd9a8dcacfad34650df2aa12079ddb6fd844fdaa778d6c5ba7376b0`
- Base image index: `python:3.9.25-slim@sha256:2d97f6910b16bd338d3060f261f53f144965f755599aab1acda1e13cf1731b1b`
- Effective UID:GID: `1000:1000`

Reproducible build command:

```bash
docker build \
  -t localhost:5000/mergepilot/test-runner-py:1.0.0 \
  -f skills/test_runner/Dockerfile .
```

The Dockerfile pins the same base-image index digest by default. The final image
was pushed to the local release registry before the E2E run. Machine-readable
build inputs, image identifiers, and `pip freeze` are in `image-build.json`.

## Test matrix

`tests/m4c/conftest.py::EXPECTED_PASS = 87`:

- sast-scan: 33
- test-runner: 49
- integration: 5

Two stable M4-C rounds completed with `passed=87 failed=0 rc=0`. Regression
coverage completed with M4-A `75/75` twice and M4-B `96/96` twice.

## Release gates

The gate verifies Python compilation, Draft 2020-12 schema validity, rules and
profile conformance, exact pytest counts, protected boundaries, whitespace,
full delivery scanning, documentation scanning, cache residue, the four-scenario
E2E JSON contract, and verification self-scan. All generated evidence is LF-only.

Historical temporary-directory counts remain `mp-artifacts-*=74` and
`mp-sandbox-*=0`; this delivery does not create either directory family.
Post-E2E checks report zero `mp-tr-*` containers and zero `mp-vol-*` volumes.

## Dependencies

The host Skill runtime adds no dependency beyond the M4-A runtime. The production
TestRunner image and offline advisory data are recorded in `THIRD_PARTY.md`.
No Semgrep, Gitleaks, Trivy, OSV client, Nacos client, or LLM runtime dependency
is introduced.

## Release structure

```text
871351a (origin/main)
  -> D  M4-C delivery: skills/sast_scan, skills/test_runner, tests/m4c,
                       evidence/m4/m4c
       tag m4c-sast-test-closed -> D
  -> G  docs: THIRD_PARTY.md, docs/附录B-Skill清单.md,
               docs/项目状态.md, docs/复赛路线图.md
```

The intended publication is linear and non-force. Existing tags remain in
place. No commit, tag, or push has been performed by this worktree.
