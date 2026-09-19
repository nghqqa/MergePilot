# gh-pr2-fix-1 — Minimal fix for confirmed CWE-22 path traversal (attempt 1)

- **Run ID**: run-gh-pr2-1414edbe-052650
- **Role**: Fixer
- **Human security gate**: APPROVED (2026-09-19T05:29:44Z)
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo (read-only)
- **PR**: #2 — stays OPEN
- **Head SHA verified**: `1414edbe513620262b31372ed1af4027be5888d1` (`git rev-parse HEAD` = same)
- **Clone**: `~/ghwork-fix-gh-pr2-1414edbe` (new directory; no `rm -rf` used)
- **Scope confirmed**: merge-base diff → 3 files, +124/-0 (M `README.md` +2 docs-only; A `backend/src/interfaces/api/v1/demo_high_risk.py` +42; A `backend/tests/unit/test_demo_high_risk_path_traversal.py` +80)
- **File changed (only)**: `backend/src/interfaces/api/v1/demo_high_risk.py`
- **Tests modified**: none (`backend/tests/unit/test_demo_high_risk_path_traversal.py` frozen)
- **GitHub writes**: zero (no push / PR / comment / merge)
- **Patch**: `shared/tasks/gh-pr2-fix-1/attempt-1.diff`
- **Patch sha256**: `674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081`

## Before → After

### Before (vulnerable, head SHA)
```python
async def demo_download(name: str) -> FileResponse:
    # VULNERABLE (intentional): no containment check on the resolved path.
    file_path = os.path.join(DEMO_FILES_DIR, name)
    return FileResponse(file_path, filename=name)
```
`name` was joined to the base directory with no normalization or containment check, so
`../` sequences escaped `DEMO_FILES_DIR` and read arbitrary process-readable files
(HTTP 200, e.g. `name=../../../etc/hostname`).

### After (fixed)
```python
async def demo_download(name: str) -> FileResponse:
    # FIX (CWE-22): normalise the request path and enforce containment within
    # the base directory. Any escape is rejected; missing files return 404.
    base_real = os.path.realpath(DEMO_FILES_DIR)
    file_real = os.path.realpath(os.path.join(base_real, name))
    if os.path.commonpath([base_real, file_real]) != base_real:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not os.path.isfile(file_real):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_real, filename=name)
```

## Three required explanations

1. **Path normalization**: `os.path.realpath()` is applied to both the base directory and
   the joined request path. It canonicalizes the path (resolving `..`, `.`, and symlinks),
   collapsing traversal sequences to a canonical absolute path before any decision.
2. **Directory boundary check**: `os.path.commonpath([base_real, file_real]) != base_real`
   enforces containment. The canonicalized request path must have the canonicalized base
   directory as its common path prefix; anything outside the base (parent, sibling,
   absolute target) fails and is rejected. `commonpath` is separator-aware and avoids the
   `/base` vs `/base-evil` string-prefix pitfall of `startswith`.
3. **Error strategy**: traversal/escape → **HTTP 400** `{"detail":"Invalid file path"}`
   (rejected input; escaped content is never served). Missing or non-file target inside
   the base → **HTTP 404** `{"detail":"File not found"}`. Legitimate in-base files →
   **HTTP 200** with content.

## Self-check evidence (real output)

```
$ /opt/venv/standard/bin/python -m py_compile backend/src/interfaces/api/v1/demo_high_risk.py
PY_COMPILE: PASS

$ git status --short
 M backend/src/interfaces/api/v1/demo_high_risk.py      # only the one target file

$ cd ~/ghwork-fix-gh-pr2-1414edbe && REPO_ROOT="$(pwd)" /opt/venv/standard/bin/python /tmp/fix_accept_check_ghpr2.py
base (realpath): /tmp/fixer-accept-v93z9uym/demo-files

[PASS] (b) legit in-base file     'ok.txt'                            -> 200 (exp 200) body='LEGIT-INSIDE-BASE'
[PASS] (a) traversal ../ escapes  '../outside-secret.txt'             -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (a) traversal via sub dir  'sub/../../deep-secret.txt'         -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (a) absolute file escape   '../../../../../../etc/hostname'    -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (c) missing file           'missing.txt'                       -> 404 (exp 404) body='{"detail":"File not found"}'

ACCEPTANCE: PASS
```

All three acceptance criteria are satisfied. Verification used throwaway temp files only;
no real credentials.

## Notes on environment and task inputs

- The repo conftest requires non-empty Google OAuth env values to import. My self-check
  loads `demo_high_risk.py` directly via `importlib`, bypassing the conftest, so it is
  unaffected. No repo files were modified.
- `~/task/ROLE-CONTRACT.md` and `~/task/PR-METADATA.md` are present, but `PR-METADATA.md`
  is a **stale** copy for a different run (`run-elem-pr1sk5-20260919-01`, PR #1, head
  `4cd5bf0`). I did **not** follow it. The authoritative parameters come from the task spec:
  PR #2, head SHA `1414edbe513620262b31372ed1af4027be5888d1`, verified by `git rev-parse HEAD`.
  The role contract (v1.0, frozen) was followed.

## Role-contract compliance note

Authorization: work began only after an explicit @mention delegation naming me and the
taskId (`gh-pr2-fix-1`), with the human security gate APPROVED (2026-09-19T05:29:44Z).
No un-delegated work was performed in this run.
