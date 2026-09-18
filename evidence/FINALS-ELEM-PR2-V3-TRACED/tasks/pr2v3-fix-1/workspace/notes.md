# pr2v3-fix-1 — Minimal fix for confirmed CWE-22 path traversal (attempt 1)

- **Run ID**: run-elem-pr2v3-20260917-01
- **Role**: Fixer
- **Human security gate**: APPROVED (2026-09-17T03:13:25Z)
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo (read-only)
- **PR**: #2 — stays OPEN
- **Head SHA verified**: `1dedf5e1992c950557064d8f4fb9039d1523deb3` (`git rev-parse HEAD` = same)
- **Clone**: `~/pr2v3-fix-work` (new directory; no `rm -rf` used)
- **Scope confirmed**: `git diff 4cd5bf0..HEAD --stat` → 2 files, +122/-0
- **File changed (only)**: `backend/src/interfaces/api/v1/demo_high_risk.py`
- **Tests modified**: none (`backend/tests/unit/test_demo_high_risk_path_traversal.py` frozen)
- **GitHub writes**: zero (no push / PR / comment / merge)
- **Patch**: `shared/tasks/pr2v3-fix-1/attempt-1.diff`
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

$ cd ~/pr2v3-fix-work && REPO_ROOT="$(pwd)" /opt/venv/standard/bin/python /tmp/fix_accept_check_v3.py
base (realpath): /tmp/fixer-accept-1h1vncq_/demo-files

[PASS] (b) legit in-base file     'ok.txt'                            -> 200 (exp 200) body='LEGIT-INSIDE-BASE'
[PASS] (a) traversal ../ escapes  '../outside-secret.txt'             -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (a) traversal via sub dir  'sub/../../deep-secret.txt'         -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (a) absolute file escape   '../../../../../../etc/hostname'    -> 400 (exp 400) body='{"detail":"Invalid file path"}'
[PASS] (c) missing file           'missing.txt'                       -> 404 (exp 404) body='{"detail":"File not found"}'

ACCEPTANCE: PASS
```

All three acceptance criteria are satisfied. Verification used throwaway temp files only;
no real credentials.

## Optional RAG consultation (per manifest)

Queried the optional `rag_retrieve` tool (SYNTHETIC org security standards, references
only): returned `org-standards/cwe-22-path-traversal.md#1`,
`org-standards/file-path-containment.md#2`, `org-standards/file-path-containment.md#1` —
consistent with the CWE-22 rating and the normalize + containment remediation approach.
Per the manifest, these references did **not** replace my own verification; the fix
decision and all PASS/FAIL evidence above come from my own reproduction.

## Role-contract compliance note

Authorization: work began only after an explicit @mention delegation naming me and the
taskId (`pr2v3-fix-1`), with the human security gate APPROVED (2026-09-17T03:13:25Z).
No un-delegated work was performed in this run. `~/task/ROLE-CONTRACT.md` and
`~/task/PR-METADATA.md` were present and consistent with this task.
