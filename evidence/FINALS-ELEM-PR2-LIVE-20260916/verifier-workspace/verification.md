# pr2-verify-1 — Independent clean-workspace verification of fix patch (attempt 1)

- **run_id**: run-elem-fastapi-pr2-20260916-01
- **Role**: Verifier (independent)
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #2 (OPEN) — zero GitHub writes performed
- **head SHA**: `1dedf5e1992c950557064d8f4fb9039d1523deb3` (verified `git rev-parse HEAD` = same) on my OWN fresh clone `~/pr2-verify-work`
- **Prerequisite clone also verified**: pristine `~/pr2-verify-prefix` at same SHA (for pre-fix baseline)
- **Patch verified**: `shared/tasks/pr2-fix-1/attempt-1.diff`
  - **sha256** = `674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081` — **matches Fixer's claim** (independent `sha256sum`)
  - `git apply --check` exit 0; `git apply` exit 0; applied `git diff` is byte-identical to the patch (`diff` exit 0)
  - only `backend/src/interfaces/api/v1/demo_high_risk.py` modified; **`backend/tests/` untouched** (`git status --short -- backend/tests/` empty)
- **Diff scope (base..head)**: 2 files, +122/-0 (confirmed `git diff 4cd5bf0..HEAD --stat`)
- **Python**: `/opt/venv/standard/bin/python` (3.11.15); deps installed: fastapi 0.141.1, httpx 0.28.1, pytest 9.1.1, pytest-asyncio 1.4.0

## VERDICT: VERIFIED (PASS)

---

## A. Pre-fix baseline (pristine clone, NO patch applied)

Independent probe `workspace/verify_probe.py` (loads the real module via importlib, mounts on a
fresh FastAPI app with `TestClient`, monkeypatches `DEMO_FILES_DIR` at runtime; lives OUTSIDE the
repo; modifies NO repo test):

```
$ cd ~/pr2-verify-work && REPO_ROOT="$(pwd)" /opt/venv/standard/bin/python <...>/verify_probe.py
[i] /etc/hostname       : 'b101f95a7d3b'

[PASS] legit in-base file     'ok.txt'                          -> 200  body='LEGIT-INSIDE-BASE'
[FAIL] traversal ../          '../outside/outside-secret.txt'   -> 200  body='TOP-SECRET-OUTSIDE-BASE'  !! LEAK
[FAIL] traversal via parent   '../deep-secret.txt'              -> 200  body='DEEP-SECRET'              !! LEAK
[FAIL] absolute-target        '../../../../../../etc/hostname'  -> 200  body='b101f95a7d3b\n'           !! LEAK
[FAIL] missing in-base file   'does-not-exist.txt'             -> 500  'Internal Server Error'
PROBE_ACCEPTANCE: FAIL   (exit=1)
```

Pre-fix repo test (pristine 2nd clone `~/pr2-verify-prefix`, `--noconftest -o markers=security_demo`):

```
1 failed, 1 passed, 1 warning in 0.42s
- test_high_risk_path_traversal_reproduces  PASSED   (leak returned OUTSIDE base; 200)
- test_high_risk_path_traversal_depth       FAILED   RuntimeError: File at path
    /tmp/pytest-of-root/.../demo-files/sub/../../../deep-secret.txt does not exist.
```

This matches the Reviewer's recorded pre-fix output (test-fixture mismatch on test 2; the
vulnerability is present regardless). **Conclusion: the probe/harness genuinely detects the flaw.**

## B. Post-fix behavior (patch applied to `~/pr2-verify-work`)

```
$ cd ~/pr2-verify-work && REPO_ROOT="$(pwd)" /opt/venv/standard/bin/python <...>/verify_probe.py
[PASS] legit in-base file     'ok.txt'                          -> 200  body='LEGIT-INSIDE-BASE'
[PASS] traversal ../          '../outside/outside-secret.txt'   -> 400  body='{"detail":"Invalid file path"}'
[PASS] traversal via parent   '../deep-secret.txt'              -> 400  body='{"detail":"Invalid file path"}'
[PASS] absolute-target        '../../../../../../etc/hostname'  -> 400  body='{"detail":"Invalid file path"}'
[PASS] missing in-base file   'does-not-exist.txt'              -> 404  body='{"detail":"File not found"}'
PROBE_ACCEPTANCE: PASS   (exit=0)
```

Post-fix repo test (same invocation):

```
2 failed, 1 warning in 0.20s
- test_high_risk_path_traversal_reproduces  FAILED  AssertionError: {"detail":"Invalid file path"} / assert 400 == 200
- test_high_risk_path_traversal_depth       FAILED  AssertionError: {"detail":"Invalid file path"} / assert 400 == 200
```

**Expected assertion inversion**: the repo test asserts the VULNERABLE behavior (200 + leak);
after the fix it now returns 400 → the flaw is CLOSED. Per INSTRUCTIONS this is the expected
result and NOT a regression.

## C. py_compile

```
$ /opt/venv/standard/bin/python -m py_compile backend/src/interfaces/api/v1/demo_high_risk.py
PRE-FIX  : PASS
POST-FIX : PASS
```

## Requirement coverage

| # | Requirement | Result |
|---|-------------|--------|
| 3a | `../outside-secret.txt` rejected | **400** `Invalid file path`, no leak — PASS |
| 3a | `sub/../../deep-secret.txt` rejected | **400** `Invalid file path`, no leak — PASS |
| 3a | `../../../../../../etc/hostname` rejected | **400** `Invalid file path`, no leak — PASS |
| 3a | missing in-base file | **404** `File not found` — PASS |
| 3b | legit in-base file still 200 + content | **200** `LEGIT-INSIDE-BASE` — PASS |
| 3c | `py_compile` on changed file | PASS |
| — | patch applies cleanly; sha256 matches | PASS |
| — | tests untouched; no extra repo files changed | PASS |
| 5 | pre-fix behavior still vulnerable | PASS (3 leaks + 1 passed repo test) |
| 7 | no GitHub writes; tests unmodified | PASS |

## Notes

- The repo-level `backend/tests/conftest.py` does not import under the standard venv
  (`ModuleNotFoundError` — the Reviewer saw `crudauth`; here `pytest_asyncio` was installable,
  but conftest still fails on other imports), so the repo test was run with `--noconftest` and a
  registered `security_demo` marker — identical to the Reviewer's method. My decisive evidence is
  the independent behavior probes, which do not depend on the repo conftest at all.
- Containment logic inspected: `realpath` + `commonpath([base_real, file_real]) != base_real`,
  with a `isfile` check for 404 — correct, no `startswith` prefix pitfall.
