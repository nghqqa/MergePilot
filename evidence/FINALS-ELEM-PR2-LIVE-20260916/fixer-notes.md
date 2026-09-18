# pr2-fix-1 — Minimal fix for confirmed CWE-22 path traversal (attempt 1)

- **Run ID**: run-elem-fastapi-pr2-20260916-01
- **Role**: Fixer
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **Head SHA verified**: `1dedf5e1992c950557064d8f4fb9039d1523deb3` (`git rev-parse HEAD` = same)
- **File changed (only)**: `backend/src/interfaces/api/v1/demo_high_risk.py`
- **Tests modified**: none (`backend/tests/unit/test_demo_high_risk_path_traversal.py` frozen)
- **GitHub writes**: zero (no push / PR / comment / merge)
- **Patch**: `shared/tasks/pr2-fix-1/attempt-1.diff`
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
   the joined result. `realpath` canonicalizes the path (resolving `..`, `.`, and
   symlinks), so `../` sequences and symlink escapes are collapsed before any check.
   The join is `os.path.join(base_real, name)` — escaping is neutralized by the
   canonicalization that follows, then rejected by the boundary check.
2. **Directory boundary check**: `os.path.commonpath([base_real, file_real]) != base_real`
   enforces containment. The canonicalized request path must have the canonicalized base
   directory as its common path prefix; any result outside the base (sibling, parent, or
   absolute target) fails the comparison and is rejected. Using `commonpath` (rather than
   a string `startswith`) avoids the `/base` vs `/base-evil` prefix-matching pitfall.
3. **Error strategy**: traversal/escape → **HTTP 400** (`{"detail":"Invalid file path"}`),
   explicitly signalling a rejected input and never serving escaped content.
   Missing/non-file target inside the base → **HTTP 404** (`{"detail":"File not found"}`).
   Legitimate in-base files → **HTTP 200** with content. No content outside the base
   directory is ever returned.

## Self-check evidence (real output)

```
$ /opt/venv/standard/bin/python -m py_compile backend/src/interfaces/api/v1/demo_high_risk.py
PY_COMPILE: PASS

$ cd ~/pr2-fix-work2 && REPO_ROOT="$(pwd)" /opt/venv/standard/bin/python /tmp/fix_accept_check.py
base (realpath): /tmp/fixer-accept-q7lxj0iu/demo-files

[PASS] (b) legit in-base file     'ok.txt'                            -> 200 (exp 200) body='LEGIT-INSIDE-BASE'
[PASS] (a) traversal ../ escapes  '../outside-secret.txt'             -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (a) traversal via sub dir  'sub/../../deep-secret.txt'         -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (a) absolute file escape   '../../../../../../etc/hostname'    -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (c) missing file           'missing.txt'                       -> 404 (exp 404) body='{"detail":"File not found"}'

ACCEPTANCE: PASS
```

All three acceptance criteria from INSTRUCTIONS.md are satisfied. Verification used
throwaway temp files only; no real credentials involved.
