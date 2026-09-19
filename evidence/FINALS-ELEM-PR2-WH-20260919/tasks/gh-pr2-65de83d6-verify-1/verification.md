# gh-pr2-65de83d6-verify-1 — Independent clean-workspace verification (attempt 1)

- **run_id**: run-gh-pr2-65de83d6-085058
- **Role**: Verifier (independent)
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #2 (OPEN) — zero GitHub writes performed
- **Human security gate**: APPROVED (2026-09-19T09:11:41Z) — remediation authorized
- **head SHA**: `65de83d6d061413ec98c1e79515f470313ef9806` (verified `git rev-parse HEAD` = same) on my OWN fresh clone `~/65de83d6-verify-work`; pristine comparison clone `~/65de83d6-verify-prefix` at same SHA; merge-base base `4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c`
- **Diff scope (base..head)**: 3 files, +132/-0 (M `README.md` +10 comment-only re-run triggers; A `demo_high_risk.py` +42; A test +80)
- **Patch verified**: `shared/tasks/gh-pr2-65de83d6-fix-1/attempt-1.diff`
  - **sha256** = `674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081` — **matches the fixer's claim** (independent `sha256sum`)
  - `git apply --check` exit 0; `git apply` exit 0; applied `git diff` is **byte-identical** to the patch (`diff` exit 0)
  - only `backend/src/interfaces/api/v1/demo_high_risk.py` modified; **`backend/tests/` untouched** (`git status --short -- backend/tests/` empty)
- **Python**: `/opt/venv/standard/bin/python` (3.11.15); deps: fastapi 0.141.1, httpx 0.28.1, pytest 9.1.1
- **Task-instance note**: verified under the run-scoped ID `gh-pr2-65de83d6-verify-1` (project `elemiso-gh-pr2-65de83d6`). This project's IDs were replanned to be collision-free from the earlier `elemiso-gh-pr2-1414edbe` run; no earlier instance was resumed.

## VERDICT: VERIFIED (PASS)

---

## A. Pre-fix baseline (PRISTINE clone, no patch) — vulnerability present

Independent probe `workspace/verify_probe.py` (loads the real module via importlib, mounts a fresh
FastAPI app + `TestClient`, monkeypatches `DEMO_FILES_DIR` at runtime; lives OUTSIDE the repo;
modifies NO repo test; direct import also bypasses the repo conftest):

```
[i] /etc/hostname       : '79f1521fbf9f'
[PASS] legit in-base file         'ok.txt'                              -> 200  body='LEGIT-INSIDE-BASE'
[FAIL] traversal ../              '../outside/outside-secret.txt'       -> 200  body='TOP-SECRET-OUTSIDE-BASE'  !! LEAK
[FAIL] nested ../ via sub dir     'sub/../../outside/outside-secret.txt'-> 200  body='TOP-SECRET-OUTSIDE-BASE'  !! LEAK
[FAIL] absolute-target traversal  '../../../../../../etc/hostname'      -> 200  body='79f1521fbf9f\n'           !! LEAK
[FAIL] missing in-base file       'does-not-exist.txt'                  -> 500  'Internal Server Error'
PROBE_ACCEPTANCE: FAIL   (exit=1)
```

Pre-fix PR test (pristine clone, `--noconftest -o markers=security_demo`):

```
1 failed, 1 passed, 2 warnings in 0.48s
- test_high_risk_path_traversal_reproduces  PASSED   (leak from OUTSIDE base returned; 200)
- test_high_risk_path_traversal_depth       FAILED   RuntimeError: File at path
    /tmp/pytest-of-root/.../demo-files/sub/../../../deep-secret.txt does not exist.
```

**The harness genuinely detects the flaw.**

## B. Post-fix behavior (patch applied to `~/65de83d6-verify-work`)

```
[PASS] legit in-base file         'ok.txt'                              -> 200  body='LEGIT-INSIDE-BASE'
[PASS] traversal ../              '../outside/outside-secret.txt'       -> 400  body='{"detail":"Invalid file path"}'
[PASS] nested ../ via sub dir     'sub/../../outside/outside-secret.txt'-> 400  body='{"detail":"Invalid file path"}'
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
| 4a | PRISTINE leaks (harness detects vuln) | `../` relative, nested `sub/../../`, and absolute-target traversal ALL → 200 with outside content — PASS |
| 4b | post-fix `../` relative rejected | **400** `Invalid file path`, no out-of-base content — PASS |
| 4b | post-fix nested `../` rejected | **400** `Invalid file path` — PASS |
| 4b | post-fix absolute-target traversal rejected | **400** `Invalid file path` — PASS |
| 4b | post-fix missing in-base file | **404** `File not found` — PASS |
| 4b | post-fix legit in-base file | **200** `LEGIT-INSIDE-BASE` — PASS |
| 4c | py_compile pre & post | PASS |
| 3 | patch applies cleanly; sha256 matches | PASS |
| 6 | tests untouched; only target file changed; zero GitHub writes | PASS |

## Deterministic skill tools (advisory only, per spec)

- **`skill_sast_scan` POST-FIX** (`req-343f24f275`, inline on the patched module): **0 findings** —
  no `AST_DANGEROUS_*` / secret / path rule fires; the vulnerable `os.path.join`+`FileResponse` sink
  is gone. Reinforces **CWE-22 closed**.
- **`skill_diff_parse`** (`req-c9d18421d0`): matched scope exactly — 3 files, +132/-0, categories
  `source`+`test`+`documentation`.
- **`skill_risk_classify`** (`req-fecf3e0db2`): advisory **L1** (`SOURCE_CONFIG_CHANGE`, recommends
  `HUMAN_REVIEW`) — metadata-only and understated; it does not read code. My independent inspection
  + POST-FIX SAST 0-findings establish the flaw is fixed. Tool is `advisory_only: true`.
- **`rag_retrieve`** unavailable this run. Optional; did not replace my own reproduction.

## Notes

- **README +10 lines** are comment-only webhook / full-toolchain / final-evidence / final-rag /
  complete-toolchain re-run triggers — no security impact.
- **Environment:** the repo-level `backend/tests/conftest.py` requires non-empty Google OAuth env
  values to import. I ran the PR test with `--noconftest` + registered `security_demo` marker; my
  decisive evidence is the independent behavior probe, which loads `demo_high_risk.py` directly via
  importlib and bypasses the conftest entirely. No repo file was modified.
- Containment logic inspected: `realpath` + `commonpath([base_real, file_real]) != base_real`
  with an `isfile` 404 path — correct; avoids the `startswith` `/base` vs `/base-evil` prefix pitfall.
- The patch (byte-identical, same sha256) is the same accepted fix reproduced on head `65de83d6`;
  this independent re-run reconfirms effectiveness under the run-scoped instance
  `gh-pr2-65de83d6-verify-1`.
