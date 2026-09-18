# Independent Security Review — PR #2 (run-elem-pr2sk3-20260918-01)

- **Role**: Reviewer (independent)
- **Run ID**: run-elem-pr2sk3-20260918-01
- **Project**: elemiso-pr2sk3-gate
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
- **SEVERITY: HIGH** (my rating, based on my own code review + reproduction)
- **CWE-22** — Improper Limitation of a Pathname to a Restricted Directory ('Path Traversal')
  (enables arbitrary file read → also CWE-73 / CWE-200 exposure)
- **HUMAN_VERIFICATION_REQUIRED: YES** — human-gate scenario; remediation only after explicit human
  security approval.

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
cd /root/.copaw-worker/reviewer && /opt/venv/standard/bin/python pr2sk3-review-repro.py
```

PR test file (deps installed; OAuth dummy env + marker registered — see §5):

```
cd /root/.copaw-worker/reviewer/pr2sk3-work && OAUTH_GOOGLE_CLIENT_ID=dummy-id OAUTH_GOOGLE_CLIENT_SECRET=dummy-secret /opt/venv/standard/bin/python -m pytest backend/tests/unit/test_demo_high_risk_path_traversal.py -q -o markers=security_demo
```

HTTP-level:

```
curl -s 'http://127.0.0.1:8000/demo-security/demo/download?name=../../../etc/hostname'
```

## 4. Real output summary

### Independent reviewer PoC (`pr2sk3-review-repro.py`)
```
[i] allowed base dir : /tmp/pr2sk3-repro-la93u50r/demo_files_base
[i] outside secret   : /tmp/pr2sk3-repro-la93u50r/outside-secret.txt
[+] name              : ../outside-secret.txt
[+] HTTP status       : 200
[+] body              : 'TOP-SECRET-OUTSIDE-BASE'
[+] escaped base?     : True
[+] LEAK CONFIRMED    : True
[*] arbitrary read    : name=../../../etc/hostname -> status=200, bytes=13, body='fcb51aca9055'
```

### PR's own test file
As written (`-q`): **collection error** — `'security_demo' not found in markers configuration option`
(the new test uses `@pytest.mark.security_demo`, unregistered in pyproject, and config sets
`--strict-markers`). With the marker registered (`-o markers=security_demo`):
```
test_high_risk_path_traversal_reproduces PASSED
test_high_risk_path_traversal_depth      FAILED
E   RuntimeError: File at path /tmp/pytest-of-root/pytest-0/test_high_risk_path_traversal_1/demo-files/sub/../../../deep-secret.txt does not exist.
=================== 1 failed, 1 passed, 3 warnings in 0.37s ====================
```
- Test 1 (`../outside/outside-secret.txt`) **PASSED** → endpoint returned a file from OUTSIDE the
  base dir (leak reproduced; assertion encodes the *vulnerable* behavior).
- Test 2 (`../../../deep-secret.txt`) **FAILED** — a fixture/payload off-by-one (that payload
  resolves to `/tmp/deep-secret.txt`, overshooting the tmp dir; the correct `../../deep-secret.txt`
  returns the secret). **Not** a mitigation.

## 5. Deterministic skill tools (advisory only)

Per the manifest I used the deterministic skills on the real PR diff:
- `skill_diff_parse` (request_id `req-abfaea0718`): 2 files, +122/-0, categories `source`+`test`,
  modules `backend/src/interfaces/api/v1`, `backend/tests/unit`. Matches the `--stat` scope.
- `skill_risk_classify` (request_id `req-80c4ce0590`): returned **L1** (`SOURCE_CONFIG_CHANGE`),
  recommended controls `AUTO_REVIEW_ELIGIBLE`, `HUMAN_REVIEW`.
  **Important**: this advisory classification is based on file *metadata only*, not the code, and
  it **understates** the real risk. My independent code review + reproduction establish an actual
  **HIGH** path traversal (arbitrary file read / CWE-22). Per the manifest, these tools are advisory
  and do not replace my own judgment; I do not adopt L1 as the rating.
- `skill_case_retrieval`: **unavailable** (`CASE_RETR_DB_UNAVAILABLE`) — optional; no effect.
- `rag_retrieve` (SYNTHETIC org standards): `org-standards/cwe-22-path-traversal.md#1`,
  `org-standards/file-path-containment.md#1` — consistent with my CWE-22 rating (references only).

## 6. Environment notes

- The repo `backend/tests/conftest.py` imports the full app; the OAuth provider factory raises
  `ValueError: The 'google' OAuth provider needs a client_secret` when `OAUTH_GOOGLE_CLIENT_SECRET`
  is empty. I supplied dummy values via **shell environment variables only** (no repo file modified).
- The new test fails at collection as written due to the unregistered marker + `--strict-markers`.
- Repo test env needs a large dependency set; no Docker socket in this container.

## 7. Secondary observations (no fix code)

- The PR's new test uses an unregistered marker + `--strict-markers` → fails at collection as written.
- `test_high_risk_path_traversal_depth` is off-by-one and fails for the wrong reason.
