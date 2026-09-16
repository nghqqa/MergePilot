# pr2r3t-verify-1 — Independent clean-workspace verification of the authorized fix patch (attempt 1)

- **run_id**: run-elem-pr2r3t-20260916-01
- **Role**: Verifier (independent)
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #2 (OPEN) — zero GitHub writes performed
- **Human security gate**: APPROVED (remediation authorized)
- **head SHA**: `1dedf5e1992c950557064d8f4fb9039d1523deb3` (verified `git rev-parse HEAD` = same) on my OWN fresh clone `~/pr2r3t-verify-work`; pristine comparison clone `~/pr2r3t-verify-prefix` at same SHA
- **Diff scope (base..head)**: 2 files, +122/-0 (confirmed `git diff 4cd5bf0..HEAD --stat`)
- **Patch verified**: `shared/tasks/pr2r3t-fix-1/attempt-1.diff`
  - **sha256** = `674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081` — **matches the fixer's claim** (independent `sha256sum`)
  - `git apply --check` exit 0; `git apply` exit 0; applied `git diff` is **byte-identical** to the patch (`diff` exit 0)
  - only `backend/src/interfaces/api/v1/demo_high_risk.py` modified; **`backend/tests/` untouched** (`git status --short -- backend/tests/` empty)
- **Python**: `/opt/venv/standard/bin/python` (3.11.15); deps: fastapi 0.141.1, httpx 0.28.1, pytest 9.1.1

## VERDICT: VERIFIED (PASS)

---

## A. Pre-fix baseline (PRISTINE clone, no patch) — vulnerability present

Independent probe `workspace/verify_probe.py` (loads the real module via importlib, mounts a fresh
FastAPI app + `TestClient`, monkeypatches `DEMO_FILES_DIR` at runtime; lives OUTSIDE the repo;
modifies NO repo test):

```
[i] /etc/hostname       : '87abd99d1ee2'
[PASS] legit in-base file         'ok.txt'                              -> 200  body='LEGIT-INSIDE-BASE'
[FAIL] traversal ../              '../outside/outside-secret.txt'       -> 200  body='TOP-SECRET-OUTSIDE-BASE'  !! LEAK
[FAIL] traversal via parent       '../deep-secret.txt'                  -> 200  body='DEEP-SECRET'              !! LEAK
[FAIL] nested ../ via sub dir     'sub/../../outside/outside-secret.txt'-> 200  body='TOP-SECRET-OUTSIDE-BASE'  !! LEAK
[FAIL] leading-slash absolute     '/etc/hostname'                       -> 200  body='87abd99d1ee2\n'           !! LEAK
[FAIL] absolute-target traversal  '../../../../../../etc/hostname'      -> 200  body='87abd99d1ee2\n'           !! LEAK
[FAIL] missing in-base file       'does-not-exist.txt'                  -> 500  'Internal Server Error'
PROBE_ACCEPTANCE: FAIL   (exit=1)
```

Note: the leading-slash vector leaks because `os.path.join(base, "/etc/hostname")` returns the
absolute `/etc/hostname` (an absolute component discards the base). All 5 escape vectors leak.

Pre-fix PR test (pristine clone, `--noconftest -o markers=security_demo`):

```
1 failed, 1 passed, 2 warnings in 0.41s
- test_high_risk_path_traversal_reproduces  PASSED   (leak from OUTSIDE base returned; 200)
- test_high_risk_path_traversal_depth       FAILED   RuntimeError: File at path
    /tmp/pytest-of-root/.../demo-files/sub/../../../deep-secret.txt does not exist.
```

**The harness genuinely detects the flaw.**

## B. Post-fix behavior (patch applied to `~/pr2r3t-verify-work`)

```
[PASS] legit in-base file         'ok.txt'                              -> 200  body='LEGIT-INSIDE-BASE'
[PASS] traversal ../              '../outside/outside-secret.txt'       -> 400  body='{"detail":"Invalid file path"}'
[PASS] traversal via parent       '../deep-secret.txt'                  -> 400  body='{"detail":"Invalid file path"}'
[PASS] nested ../ via sub dir     'sub/../../outside/outside-secret.txt'-> 400  body='{"detail":"Invalid file path"}'
[PASS] leading-slash absolute     '/etc/hostname'                       -> 400  body='{"detail":"Invalid file path"}'
[PASS] absolute-target traversal  '../../../../../../etc/hostname'      -> 400  body='{"detail":"Invalid file path"}'
[PASS] missing in-base file       'does-not-exist.txt'                  -> 404  body='{"detail":"File not found"}'
PROBE_ACCEPTANCE: PASS   (exit=0)
```

Post-fix PR test (same invocation):

```
2 failed, 2 warnings in 0.21s
- test_high_risk_path_traversal_reproduces  FAILED  AssertionError: {"detail":"Invalid file path"} / assert 400 == 200
- test_high_risk_path_traversal_depth       FAILED  AssertionError: {"detail":"Invalid file path"} / assert 400 == 200
```

**Expected assertion inversion** (the PR test asserts the PRE-FIX vulnerable 200 behavior; after the
patch it is 400) — the flaw is CLOSED, not a regression (per spec step 5).

## C. py_compile

```
PRE-FIX  : PASS
POST-FIX : PASS
```

## Requirement coverage

| # | Requirement | Result |
|---|-------------|--------|
| 4a | PRISTINE leaks (harness detects vuln) | `../` relative, nested `sub/../../`, leading-slash `/etc/hostname`, and absolute-target traversal ALL → 200 with outside content — PASS |
| 4b | post-fix `../` relative rejected | **400** `Invalid file path` — PASS |
| 4b | post-fix nested `../` rejected | **400** `Invalid file path` — PASS |
| 4b | post-fix leading-slash absolute rejected | **400** `Invalid file path` — PASS |
| 4b | post-fix absolute-target traversal rejected | **400** `Invalid file path` — PASS |
| 4b | post-fix missing in-base file | **404** `File not found` — PASS |
| 4b | post-fix legit in-base file | **200** `LEGIT-INSIDE-BASE` — PASS |
| 4c | py_compile pre & post | PASS |
| 3 | patch applies cleanly; sha256 matches | PASS |
| 6 | tests untouched; no extra repo files; zero GitHub writes | PASS |

## Notes

- The patch blocks the leading-slash vector because it joins against the **canonical base**
  (`os.path.join(base_real, name)`) — with `base_real` absolute, a leading `/` component no longer
  re-parents to filesystem root — then the `commonpath` containment check rejects any escape.
- The repo-level `backend/tests/conftest.py` pulls heavy app deps / Docker-based Postgres
  testcontainers, so the PR test was run with `--noconftest` + registered `security_demo` marker
  (same method as the Reviewer). Decisive evidence is the independent behavior probe, which does
  not depend on the repo conftest.
- Containment logic inspected: `realpath` + `commonpath([base_real, file_real]) != base_real`
  with an `isfile` 404 path — correct; no `startswith` `/base` vs `/base-evil` prefix pitfall.
- The patch (byte-identical, same sha256) replicates the earlier accepted fix; this independent
  re-run reconfirms it on a fresh clone, now including the leading-slash and nested vectors.
