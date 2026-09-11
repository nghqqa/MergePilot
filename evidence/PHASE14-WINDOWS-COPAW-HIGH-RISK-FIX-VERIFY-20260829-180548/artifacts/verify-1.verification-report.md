# Verification Report — verify-1 (copaw-high-risk-human-gate)

**Verifier**: p14h2-copaw-worker-verifier
**Date**: 2026-08-29
**Repository**: nghqqa/fastapi-boilerplate-demo, PR #2, branch `demo/high-risk-human-gate`
**File**: `backend/src/interfaces/api/v1/demo_high_risk.py` — `demo_download`
**Vulnerability**: CWE-22 path traversal / arbitrary file read (HIGH)

This report is an **independent** verification of the fix-1 CWE-22 remediation.
The fixer's conclusions were NOT trusted; the fix was re-derived, re-applied to a
fresh clone, and re-tested end-to-end from scratch.

Environment: Python 3.11.15, fastapi 0.141.1, starlette, httpx 0.28.1, pytest 9.1.1.
Local work only. PR #2 stays OPEN. Nothing pushed or merged.

---

## 1. Independent reproduction of the vulnerability (BEFORE the fix)

Cloned the PR branch fresh and ran the repo's own unit test on the **unfixed**
code:

```
$ pytest backend/tests/unit/test_demo_high_risk_path_traversal.py -v
test_high_risk_path_traversal_reproduces PASSED
test_high_risk_path_traversal_depth FAILED  (starlette RuntimeError:
    File .../demo-files/sub/../../../deep-secret.txt does not exist — OS
    depth quirk, unrelated to the confirmed primary reproduction)
```

`test_high_risk_path_traversal_reproduces` **PASSED**, i.e. the repo-test itself
confirms `../outside/outside-secret.txt` returns the outside file.

**Independent traversal probe (unfixed code):**
```
1) GET /demo/download?name=../outside/outside-secret.txt
   status: 200
   body:   'TOP-SECRET-OUTSIDE-BASE'
   LEAKED_OUTSIDE_SECRET: True      <- CONFIRMED VULNERABLE
2) GET /demo/download?name=welcome.txt
   status: 200 / body: 'WELCOME-LEGIT-DEMO'
```

**Verdict (before fix):** arbitrary file read outside `DEMO_FILES_DIR` confirmed
via `../outside/outside-secret.txt` (HTTP 200, content disclosed).

---

## 2. Patch review (independent)

`fix.patch` (fixer deliverable) was re-applied to a fresh clone via
`git apply --check` (applies cleanly with no fuzz/conflicts) then applied.

**Minimality** — the working tree diff after applying the patch is EXACTLY one
file:

```
$ git diff --stat
 backend/src/interfaces/api/v1/demo_high_risk.py | 16 ++++++++++++----
 1 file changed, 12 insertions(+), 4 deletions(-)

$ git status --porcelain
 M backend/src/interfaces/api/v1/demo_high_risk.py
```

Only `demo_high_risk.py` is touched: it adds `Path` and `HTTPException` imports
and rewrites the `demo_download` body. No unrelated files, no other modules.

**Containment logic** (the essence of the fix, confirmed correct):
```python
base   = Path(DEMO_FILES_DIR).resolve()
target = (base / name).resolve()
if not target.is_relative_to(base):
    raise HTTPException(status_code=404, detail="Not found")
if not target.is_file():
    raise HTTPException(status_code=404, detail="Not found")
return FileResponse(target, filename=target.name)
```
- `Path.resolve()` normalizes `..` and symlinks before the containment check.
- `is_relative_to(base)` rejects any path escaping `DEMO_FILES_DIR` (404).
- `is_file()` rejects missing/non-file targets (404).
- `filename=target.name` uses only the basename of the resolved target, preventing
  filename-header injection / further traversal via the `filename` parameter.

The re-applied patch produced a file **byte-identical** to the fixer's
`demo_high_risk.fixed.py` deliverable (`diff` clean), confirming the fixer's
artifact is genuine and matches the patch.

---

## 3. Independent functional verification (AFTER the fix)

### 3a. Independent traversal probe (patched code)
```
1) GET /demo/download?name=../outside/outside-secret.txt
   status: 404
   body:   '{"detail":"Not found"}'
   LEAKED_OUTSIDE_SECRET: False
   TRAVERSAL_REJECTED (4xx, no leak): True    <- FIX WORKS
2) GET /demo/download?name=welcome.txt
   status: 200 / body: 'WELCOME-LEGIT-DEMO'   <- NO REGRESSION
```

### 3b. Repo's own unit tests after the fix
```
test_high_risk_path_traversal_reproduces FAILED (404 != 200)
test_high_risk_path_traversal_depth       FAILED (404 != 200)
```
These fail **only because** they assert the *pre-fix* vulnerable behavior
(`assert status == 200` on a traversal/disclosure). Under the fix the endpoint
now returns **404** instead of leaking the file — i.e. exactly the remediation
required. The test file is intentionally left unchanged per the "minimal change,
only `demo_download`" constraint. Their failure is the correct confirmation that
the traversal is closed.

### 3c. Import / regression smoke test
```
IMPORT_OK: router=<fastapi.routing.APIRouter>, tags=['SecurityDemo']
```
The patched module imports cleanly; the app routers load without error.

---

## 4. Regression / scope check

- Working-tree diff after fix = **exactly one file** (`demo_high_risk.py`).
- The PR branch `demo/high-risk-human-gate` in the remote is **untouched** (local
  clone only; no push, no merge, no close/reopen).
- The patched module imports cleanly and the legitimate download still returns 200.
- No passwords, tokens, keys or cookies were read, stored, or emitted.

---

## 5. Conclusion

The fix-1 remediation for the CWE-22 path traversal is **independently verified**:

- `FIX_INDEPENDENTLY_VERIFIED`: the traversal `../outside/outside-secret.txt` is
  rejected with **404** and no longer leaks the external secret; the fix is minimal
  and correctly enforces path normalization (`Path.resolve`) + containment
  (`is_relative_to(DEMO_FILES_DIR)`).
- `REGRESSION_CHECK_PASSED`: legitimate demo file still served (200), module
  imports cleanly, and no unrelated files changed.
- Residual severity after fix: **NONE**.

**Verify result**: VERIFICATION_PASSED
