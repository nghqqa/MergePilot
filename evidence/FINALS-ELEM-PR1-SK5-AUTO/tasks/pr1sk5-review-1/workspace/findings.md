# Independent Security Review — PR #1 bootstrap (run-elem-pr1sk5-20260919-01)

- **Role**: Reviewer (independent)
- **Run ID**: run-elem-pr1sk5-20260919-01
- **Project**: elemiso-pr1sk5-auto
- **Repo**: https://github.com/nghqqa/fastapi-boilerplate-demo
- **PR**: #1 — "demo: enforce unique API-key names per user" (OPEN)
- **Head SHA verified**: `4cd5bf099f88c3f3f85ee4c06b7adfe9925a6e5c` (`git rev-parse HEAD` = same)
- **Base SHA**: `fdde4f4142606336c7b7b25f176949dc5882d89a` (main)
- **Diff scope confirmed** (`git diff fdde4f4..HEAD --stat`): **6 files, +168 / -1**
  - A `PR_DESCRIPTION.md` (+33)
  - A `backend/migrations/versions/0001_uq_api_keys_user_name.py` (+28)
  - M `backend/src/modules/api_keys/models.py` (+2/-1)
  - A `backend/tests/integration/migrations/__init__.py` (0)
  - A `backend/tests/integration/migrations/base_schema_helper.py` (+20)
  - A `backend/tests/integration/migrations/test_uq_user_key_name.py` (+85)
- **Constraints honored**: no repository file modified; no fix/patch code; zero GitHub writes; own workspace only.

## Verdict

- **STATUS: NOT_CONFIRMED** (no *new* security vulnerability introduced by this PR)
- **SEVERITY: LOW**
- **CWE**: none newly introduced for the security classes in scope. The PR contains an explicitly
  labeled **seeded reliability/data-integrity risk** (see §3) that is *declared in the diff itself*
  and is not a security vulnerability under the org CWE classes I reviewed.
- **HUMAN_VERIFICATION_REQUIRED: NO**

## 1. Scope review — what the PR actually changes

1. **Model (+1 line)**: adds `UniqueConstraint("user_id", "name", name="uq_api_keys_user_name")` to
   `APIKey.__table_args__` in `backend/src/modules/api_keys/models.py` (additive; no column, no
   type, no default change). Existing API response schemas unaffected.
2. **Migration (`demo0001`)**: `op.create_unique_constraint("uq_api_keys_user_name", "api_keys",
   ["user_id", "name"])`; `downgrade()` drops it. Static SQL DDL; no dynamic/`op.execute` raw
   string, no user input, no secret material.
3. **Tests + helper**: integration tests against a Postgres URL supplied by the harness; the helper
   emits fixed DDL for a throwaway baseline schema.
4. **`PR_DESCRIPTION.md`**: documentation only.

No executable request path, no authentication/authorization change, no new attack surface.

## 2. Security checks performed (my own analysis)

- **No injection vector**: the migration and helper use constant identifiers/table names; no string
  concatenation of external input into SQL/DDL.
- **No secrets**: no credential, token, or key material added (secret-scanner engine found none).
- **No authn/authz change**: no route, dependency, or permission logic touched.
- **No new dependency**: no manifest/lockfile change.
- **No deserialization / eval / path handling**: none present.
- **`subprocess` in tests**: the only SAST hit is `subprocess.run` in the test file (see §4). It is
  called with an **argv list**, `shell=False` (default), constant arguments, and no untrusted
  interpolation — **not** a command-injection vector. Adjudicated as a false positive.

## 3. Seeded risk that is NOT a security finding (noted for completeness, no fix code)

Both the migration docstring and `PR_DESCRIPTION.md` state the defect verbatim: adding
`UNIQUE(user_id, name)` **without de-duplicating historical rows** causes `upgrade` to fail with
`IntegrityError` (SQLSTATE 23505) on any database where a user already has same-named keys (which
was legal before this change).

- This is a **reliability / data-migration-safety** issue, not a security vulnerability (no
  confidentiality/integrity/availability of *other* parties' data is compromised by an attacker).
- It is **intentionally seeded and disclosed** in the PR: `PR_DESCRIPTION.md` names the reproducer
  `test_historical_duplicates_upgrade_fails` "(this test FAILS by design until the fix lands)".
- Because it is explicitly declared and out of the security classes in scope, it does **not**
  change my security verdict. I record it only because it is the material non-security risk in the
  diff. No fix is proposed (contract forbids patch code).

## 4. Tools run (real output)

### `skill_sast_scan` (inline mode; `request_id` `req-c2c37caad4`)
```
files_scanned: 4
findings_total: 1
- AST_DANGEROUS_SUBPROCESS_SHELL, severity high, L2, file
  backend/tests/integration/migrations/test_uq_user_key_name.py:9
  "dangerous call: subprocess.run"
engines_used: ["ast_python", "dep_vuln", "secret"]
```
Adjudication: **false positive** — the call passes an argv list (`[sys.executable, "-m", "alembic",
...]`) with `shell=False` and constant arguments; no interpolation of untrusted input.
(`paths` mode returned `SAST_SCAN_TRUSTED_CONFIG_MISSING` — deploy-provided root required, so I used
`inline` with the real file contents.)

### `skill_risk_classify` (advisory; `request_id` `req-b654cd1fda`)
```
risk_level: L1, rule SOURCE_CONFIG_CHANGE, controls [AUTO_REVIEW_ELIGIBLE, HUMAN_REVIEW]
```
Consistent with my LOW / NOT_CONFIRMED security assessment (advisory only; my judgment governs).

### PR's own tests (real output)
```
cd /root/.copaw-worker/reviewer/pr1sk5-work
OAUTH_GOOGLE_CLIENT_ID=dummy-id OAUTH_GOOGLE_CLIENT_SECRET=dummy-secret \
  /opt/venv/standard/bin/python -m pytest \
  backend/tests/integration/migrations/test_uq_user_key_name.py -v
---
test_empty_db_upgrade_succeeds SKIPPED
test_historical_duplicates_upgrade_fails SKIPPED
======================== 2 skipped, 3 warnings in 0.01s ========================
```
Both tests skip because `P14_BASELINE_TREE` is unset and `/var/run/docker.sock` is absent — no
Postgres/harness in this container. Environment limitation, not a code defect; the tests are
correctly guarded by `@needs_baseline`.

### `rag_retrieve` / `skill_case_retrieval` (optional, references only)
- `rag_retrieve` returned **0 results** for migration/schema-rollout queries (SYNTHETIC corpus,
  lexical; no matching org standard).
- `skill_case_retrieval` matched only the unrelated PR #2/#3 path-traversal & command-injection
  cases (untrusted references); **no** relevant historical bootstrap/migration case. Neither tool
  changed my verdict.

## 5. Reproduction

```
cd /root/.copaw-worker/reviewer/pr1sk5-work && \
  git diff --stat fdde4f4142606336c7b7b25f176949dc5882d89a..HEAD && \
  cat -n backend/migrations/versions/0001_uq_api_keys_user_name.py && \
  cat -n backend/src/modules/api_keys/models.py | sed -n '36,42p'
```

## 6. Conclusion

No new security vulnerability is introduced by this bootstrap PR; the materials are additive
(documentation, a static DDL migration, a model constraint, and guarded integration tests). The
only substantive non-security risk is the explicitly seeded
"upgrade fails on pre-existing duplicate rows" behavior, which the PR itself discloses and labels
as failing-by-design pending a fix. Security verdict: **NOT_CONFIRMED / LOW**.
