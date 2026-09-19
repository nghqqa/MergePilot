# Project Result: PR #2 webhook review (re-run)

- **Project ID**: elemiso-gh-pr2-e9731abd
- **Run ID**: run-gh-pr2-e9731abd-064416
- **Requester**: admin (Team Admin, via Leader DM)
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #2 (head SHA `e9731abd95d72e4d09f36060dcb32cdef50474d0`, merge-base base `4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c`)
- **Status**: COMPLETE — all DAG nodes accepted
- **Human security gate**: APPROVED (2026-09-19T06:51:04Z; remediation authorized)

## Outcome

A HIGH-severity CWE-22 path traversal was independently confirmed, a minimal
containment fix was produced and delivered locally after explicit human gate
approval, and the fix was independently verified in a clean workspace. PR #2
remains OPEN; **zero GitHub writes** were performed. This run supersedes the
prior PR #2 run at head `1414edbe`.

## DAG Execution

- [x] `gh-pr2-review-1` — Independent security review (reviewer) — **FINDING_CONFIRMED / HIGH / CWE-22**
- [x] `elemiso-gh-pr2-e9731abd-fix-1` — Minimal local fix + patch delivery (fixer) — **FIX_APPLIED / SELF_CHECK_PASSED**
- [x] `elemiso-gh-pr2-e9731abd-verify-1` — Independent clean-workspace verification (verifier) — **VERIFIED (PASS)**

Task IDs on the fix/verify nodes are run-scoped (`elemiso-gh-pr2-e9731abd-*`) to
avoid collision with the earlier run's already-submitted `gh-pr2-fix-1` /
`gh-pr2-verify-1` records.

## Blocker and Recovery

- **Blocker**: the fixer's `ack_task` on `gh-pr2-fix-1` routed to the **earlier,
  already-submitted** instance (project `elemiso-gh-pr2-1414edbe`, head
  `1414edbe`), because task IDs are reused across runs. Per `task-management`,
  the fixer correctly refused to resume/rewrite an already-submitted task.
- **Recovery**: per `project-management`, the project DAG was replanned with the
  full retained graph, replacing the colliding node IDs with run-scoped unique
  IDs (`elemiso-gh-pr2-e9731abd-fix-1`, `elemiso-gh-pr2-e9731abd-verify-1`). The
  old submitted instance was not mutated. The fix node was re-delegated and
  completed normally.

## Key Findings

- Vulnerable: `backend/src/interfaces/api/v1/demo_high_risk.py`, function
  `demo_download` — L28 `DEMO_FILES_DIR`, L41 `os.path.join(DEMO_FILES_DIR, name)`
  (no normalization/containment), L42 `FileResponse`; no auth dependency →
  arbitrary process-readable file read (also CWE-73 / CWE-200 exposure).
- Pre-fix reproduction: `../outside-secret.txt` → HTTP 200 out-of-base content;
  nested `sub/../../outside/outside-secret.txt` and `../../../etc/hostname` →
  HTTP 200.
- The README +4 lines are comment-only webhook re-run triggers (no security
  impact).
- Deterministic skills (available this run, advisory only): `skill_diff_parse`
  matched scope (3 files +126/-0); `skill_risk_classify` → advisory **L1**
  (`SOURCE_CONFIG_CHANGE`, metadata-only, understated vs. actual HIGH);
  `skill_sast_scan` → 0 findings (semantic flaw, not a pattern hit);
  `skill_case_retrieval` → DB unavailable; `rag_retrieve` → unavailable. None
  replaced independent reproduction.

## Fix

- File changed (only): `backend/src/interfaces/api/v1/demo_high_risk.py`.
- Approach: `os.path.realpath` normalization on both base and joined path, plus
  `os.path.commonpath([base_real, file_real]) != base_real` containment check.
- Error strategy: escape → 400 `Invalid file path`; missing in-base → 404
  `File not found`; legitimate in-base file → 200.
- Tests untouched (frozen); no GitHub writes; PR #2 stays OPEN.
- Patch: `shared/tasks/elemiso-gh-pr2-e9731abd-fix-1/attempt-1.diff`
  (sha256 `674356fc9a661faa49b97b789d75ff4529b95a5fac7f39dad0301397c0116081`).

## Verification

Independent verifier reproduced the patch sha256, applied it cleanly in a fresh
clone at head `e9731abd`, confirmed the pristine checkout was vulnerable
(traversal vectors leaked out-of-base content at HTTP 200, including nested
`../` and absolute `/etc/hostname`), and confirmed all post-fix cases: legit
in-base → 200; all escape vectors → 400; missing → 404; `py_compile` PASS.
Post-fix PR test = 2 failed = expected assertion inversion (the PR's own test
asserts the pre-fix vulnerable 200 behavior), i.e. flaw closed, not a regression.

## Deliverables

- `shared/tasks/gh-pr2-review-1/workspace/findings.md` — independent review findings
- `shared/tasks/elemiso-gh-pr2-e9731abd-fix-1/attempt-1.diff` — minimal fix patch
- `shared/tasks/elemiso-gh-pr2-e9731abd-fix-1/workspace/notes.md` — fix rationale and self-check
- `shared/tasks/elemiso-gh-pr2-e9731abd-verify-1/workspace/verification.md` — independent verification
- `shared/tasks/elemiso-gh-pr2-e9731abd-verify-1/workspace/verify_probe.py` — verification probe

## Notes

- Gate discipline honored: the Leader stopped at the human security gate after
  the HIGH finding and did not delegate the fix until the Team Admin posted
  explicit approval.
- Task-ID collision across runs is a recurring risk; run-scoped IDs are the safe
  pattern for re-run projects.
- PR #2 stays OPEN. Any GitHub-side action requires explicit authorization.
