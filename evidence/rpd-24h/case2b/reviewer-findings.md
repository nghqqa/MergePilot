# Independent Security Review — PR #2 (run-gh-pr2-42ed1787-003205)

- **Role**: Reviewer (independent)
- **Run ID**: run-gh-pr2-42ed1787-003205
- **Project**: elemiso-gh-pr2-42ed1787
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #2 (webhook-triggered; OPEN, zero GitHub writes)
- **Head SHA verified**: `42ed17879becbc02e31551938afbbf689351df96` (`git rev-parse HEAD` = same)
- **Merge-base base SHA**: `4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c` (`git merge-base` = same; base drift immune)
- **Diff scope confirmed** (`git diff --stat <merge-base>..HEAD`): **3 files, +132 / -0**
  - M `README.md` (+10) — docs only (re-run trigger comments)
  - A `backend/src/interfaces/api/v1/demo_high_risk.py` (+42)
  - A `backend/tests/unit/test_demo_high_risk_path_traversal.py` (+80)
- **Constraints honored**: no repository file modified; no fix/patch code; zero GitHub writes; own workspace only.

## Verdict

- **STATUS: FINDING_CONFIRMED**
- **SEVERITY: HIGH** (my rating, from my own code review + reproduction)
- **CWE-22** — Improper Limitation of a Pathname to a Restricted Directory ('Path Traversal')
  (arbitrary file read → also CWE-73 / CWE-200 exposure)
- **HUMAN_VERIFICATION_REQUIRED: YES**

## 1. User-controlled parameter escapes the base directory — YES

- File: `backend/src/interfaces/api/v1/demo_high_risk.py`
- Function: `demo_download`
- Key lines: **L28** `DEMO_FILES_DIR = os.path.join(os.path.dirname(__file__), "demo_files_base")`;
  **L41** `file_path = os.path.join(DEMO_FILES_DIR, name)` (no normalization / no containment);
  **L42** `return FileResponse(file_path, filename=name)`. No auth dependency on the route.
- Source at this head is byte-identical to the previously reviewed vulnerable version
  (sha256 `5e87b4187e1b570508de93c88b6ed88c471f51f877e919617cfaceabd5aa2373`).

## 2. Arbitrary file read outside the base directory — YES

Sibling-directory file outside base → read; absolute file `/etc/hostname` → read. Both HTTP 200.

## 3. Reproduction

```
cd /root/.copaw-worker/reviewer && /opt/venv/standard/bin/python ghpr2g-repro.py
```
```
cd /root/.copaw-worker/reviewer/ghwork-run-gh-pr2-42ed1787-003205 && \
  OAUTH_GOOGLE_CLIENT_ID=dummy-id OAUTH_GOOGLE_CLIENT_SECRET=dummy-secret \
  /opt/venv/standard/bin/python -m pytest \
  backend/tests/unit/test_demo_high_risk_path_traversal.py -q -o markers=security_demo
```

## 4. Real output summary

### Independent reviewer PoC (`ghpr2g-repro.py`)
```
base      : /tmp/ghpr2g-rgr56syd/demo_files_base
traversal : name=../outside-secret.txt -> status=200 body='TOP-SECRET-OUTSIDE-BASE'
escaped   : True
absolute  : name=../../../etc/hostname -> status=200 bytes=13
```

### PR's own test file
As written: collection error (`security_demo` marker unregistered + `--strict-markers`).
With marker registered (`-o markers=security_demo`):
```
test_high_risk_path_traversal_reproduces PASSED
test_high_risk_path_traversal_depth      FAILED
E   RuntimeError: File at path /tmp/pytest-of-root/pytest-0/test_high_risk_path_traversal_1/demo-files/sub/../../../deep-secret.txt does not exist.
=================== 1 failed, 1 passed, 3 warnings in 0.47s ====================
```
- Test 1 PASSED → leak from OUTSIDE the base dir reproduced (HTTP 200).
- Test 2 FAILED — fixture/payload off-by-one (`../../../` overshoots to `/tmp`; correct
  `../../deep-secret.txt` returns the secret). **Not** a mitigation.

## 5. Advisory tools (per spec; none replaced my reproduction)

- `skill_diff_parse` (`req-2ac546a03b`): `demo_high_risk.py` +42/-0, category `source` — matches scope.
- `skill_sast_scan` (inline; `req-66d2816a64`): **0 findings** — the unnormalized `os.path.join` is a
  semantic flaw, not a shell/SQL pattern the AST rules detect (a **false negative**, consistent with
  prior runs). My HIGH rating stands on the reproduction.
- `rag_retrieve` (available; data_mode SYNTHETIC, references only): matched
  `org-standards/cwe-22-path-traversal.md#1` (`doc-cwe22-def`) and
  `org-standards/file-path-containment.md#1` (`doc-path-containment`) — consistent with CWE-22.
- `skill_case_retrieval` → `CASE_RETR_SCOPE_MISSING` (unavailable this run).

## 6. Notes

- `README.md` change is comment-only re-run trigger comments (`+10` docs lines) — no security impact.
- Repo `conftest.py` needs a non-empty Google OAuth secret; supplied dummy values via **shell env only**
  (no repo file modified).

## 7. Conclusion

Confirmed HIGH path traversal / arbitrary file read in `demo_download` (CWE-22). Verdict:
**FINDING_CONFIRMED / HIGH / HUMAN_VERIFICATION_REQUIRED: YES**.
