# Independent Security Review — PR #2 (run-gh-pr2-e9731abd-064416)

- **Role**: Reviewer (independent)
- **Run ID**: run-gh-pr2-e9731abd-064416
- **Project**: elemiso-gh-pr2-1414edbe
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #2 (webhook-triggered re-run; OPEN, zero GitHub writes)
- **Head SHA verified**: `e9731abd95d72e4d09f36060dcb32cdef50474d0` (`git rev-parse HEAD` = same)
- **Merge-base base SHA**: `4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c` (`git merge-base` = same; base drift immune)
- **Diff scope confirmed** (`git diff --stat <merge-base>..HEAD`): **3 files, +126 / -0**
  - M `README.md` (+4) — docs only (webhook/full-toolchain re-run trigger comments)
  - A `backend/src/interfaces/api/v1/demo_high_risk.py` (+42)
  - A `backend/tests/unit/test_demo_high_risk_path_traversal.py` (+80)
- **Constraints honored**: no repository file modified; no fix/patch code; zero GitHub writes; own workspace only.

## Verdict

- **STATUS: FINDING_CONFIRMED**
- **SEVERITY: HIGH** (my rating, from my own code review + reproduction)
- **CWE-22** — Improper Limitation of a Pathname to a Restricted Directory ('Path Traversal')
  (enables arbitrary file read → also CWE-73 / CWE-200 exposure)
- **HUMAN_VERIFICATION_REQUIRED: YES**

## 1. User-controlled parameter escapes the base directory — YES

- File: `backend/src/interfaces/api/v1/demo_high_risk.py`
- Function: `demo_download` (`async def demo_download(name: str) -> FileResponse`)
- Key lines:
  - **L28** `DEMO_FILES_DIR = os.path.join(os.path.dirname(__file__), "demo_files_base")`
  - **L41** `file_path = os.path.join(DEMO_FILES_DIR, name)` — user-controlled `name` joined with
    **no normalization and no containment check**.
  - **L42** `return FileResponse(file_path, filename=name)` — serves the resolved (escaped) path.
- `os.path.join` does not neutralize `../`. No `realpath`+`commonpath` containment, no allow-list,
  no `..`/separator filtering. No auth dependency on the route.

## 2. Arbitrary file read outside the base directory — YES

Demonstrated: a sibling-directory file outside the base dir, and an absolute system file
(`/etc/hostname`), both returned with HTTP 200 and real content.

## 3. Reproduction (one command)

```
cd /root/.copaw-worker/reviewer && /opt/venv/standard/bin/python ghpr2b-repro.py
```

PR test file (deps present; marker registered — see §5):

```
cd /root/.copaw-worker/reviewer/ghwork-run-gh-pr2-e9731abd-064416 && \
  OAUTH_GOOGLE_CLIENT_ID=dummy-id OAUTH_GOOGLE_CLIENT_SECRET=dummy-secret \
  /opt/venv/standard/bin/python -m pytest \
  backend/tests/unit/test_demo_high_risk_path_traversal.py -q -o markers=security_demo
```

## 4. Real output summary

### Independent reviewer PoC (`ghpr2b-repro.py`)
```
base      : /tmp/ghpr2b-ruflkksw/demo_files_base
traversal : name=../outside-secret.txt -> status=200 body='TOP-SECRET-OUTSIDE-BASE'
escaped   : True
absolute  : name=../../../etc/hostname -> status=200 bytes=13
```

### PR's own test file
As written (`-q`): **collection error** — `'security_demo' not found in markers configuration option`
(the test uses `@pytest.mark.security_demo`, unregistered in pyproject, and config sets
`--strict-markers`). With the marker registered (`-o markers=security_demo`):
```
test_high_risk_path_traversal_reproduces PASSED
test_high_risk_path_traversal_depth      FAILED
E   RuntimeError: File at path /tmp/pytest-of-root/pytest-1/test_high_risk_path_traversal_1/demo-files/sub/../../../deep-secret.txt does not exist.
=================== 1 failed, 1 passed, 3 warnings in 0.37s ====================
```
- Test 1 (`../outside/outside-secret.txt`) **PASSED** → endpoint returned a file from OUTSIDE the
  base dir (leak reproduced; assertion encodes the *vulnerable* behavior).
- Test 2 (`../../../deep-secret.txt`) **FAILED** — a fixture/payload off-by-one (that payload
  resolves to `/tmp/deep-secret.txt`, overshooting the tmp dir; the correct `../../deep-secret.txt`
  returns the secret). **Not** a mitigation.

## 5. Deterministic skills (advisory only; available this run)

- `skill_diff_parse` (`req-e65912f949`): 2 files, +46/-0, categories `source`+`documentation`
  (`README.md`, `demo_high_risk.py`) — consistent with the key source file in scope.
- `skill_risk_classify` (`req-85aed2c389`): advisory **L1** (`SOURCE_CONFIG_CHANGE`), controls
  `AUTO_REVIEW_ELIGIBLE`, `HUMAN_REVIEW`. **Metadata-only and understated** vs. the actual HIGH
  CWE-22; my independent review + reproduction govern.
- `skill_sast_scan` (inline, source file; `req-afd6afabb7`): **0 findings** — the unnormalized
  `os.path.join` is a semantic flaw, not a shell/SQL pattern engine hit.
- `skill_case_retrieval`: `CASE_RETR_DB_UNAVAILABLE` this run (degraded). `rag_retrieve`:
  `FunctionNotFoundError` (unavailable). Neither replaced my own reproduction.

## 6. Notes

- `README.md` change is comment-only webhook/full-toolchain re-run triggers (`+4` docs lines) — no
  security impact.
- Repo `conftest.py` needs a non-empty Google OAuth secret; supplied dummy values via **shell env
  only** (no repo file modified).

## 7. Conclusion

Confirmed HIGH path traversal / arbitrary file read in `demo_download` (CWE-22). Verdict:
**FINDING_CONFIRMED / HIGH / HUMAN_VERIFICATION_REQUIRED: YES**.
