# Security

## Authentication
- Server-side session (in-memory, restart-invalidated)
- HMAC-signed sid cookie (HttpOnly, SameSite=Strict)
- Timing-safe credential comparison
- CSRF token enforcement on side-effect methods

## Authorization
- Repository allowlist (server-side row-level filtering)
- Unauthorized repo: 403 (explicit) or invisible (aggregate)
- Unknown resource: 404 (no existence disclosure)

## Data Security
- PG/MinIO on internal network (no external access)
- Console binds to loopback by default
- Evidence directory mounted read-only
- Secrets never in images, logs, or docs

## Security Red Lines
1. BLOCKED/STALE/ACTION_REQUIRED/DEGRADED never shown as PASSED
2. No receipt never produces success
3. Stale head never passes
4. A-chain results are org-standard reference only
5. GitHub writes disabled by default
6. All responses pass redact()

## Credential Policy (2026-09-26)
**凭据不得提交。** Any real credential — password, API token, private key,
connection string with embedded credentials — must never be committed to this
repository, any branch, tag, or release asset. Placeholders and documented
synthetic examples (e.g. `AKIA…EXAMPLE`, `ghp_abcdef…`) are the only permitted
secret-shaped strings.

- Credentials are injected at runtime via environment variables or a protected
  secret store, never via files under version control.
- Local verification scripts read credentials exclusively from environment
  variables (see `verification/gate/` conventions) and fail fast when unset.
- Note: `.gitignore` does NOT untrack already-committed files. Adding a path
  to `.gitignore` after it was committed does not remove it — always verify
  with `git ls-files` before pushing.

## Secret Scanning Gates (2026-09-26)
Three enforcement layers, all backed by `scripts/secret-scan.sh`:

| Layer | Trigger | Effect |
|---|---|---|
| `pre-commit` hook | `git commit` | Staged-content scan; commit rejected on hit |
| `pre-push` hook | `git push` | Commit-tree scan; push rejected on hit |
| CI (`secret-scan.yml`) | push / PR / release publish | Full-tree + per-commit scan; red build blocks release workflow |

- Install local hooks: `bash scripts/install-hooks.sh`
- A scanner hit means commit, push, and release are all forbidden until the
  finding is resolved. There is no "acknowledge and continue" path.
- Exemptions (e.g. a new documented synthetic fixture) require a manual edit to
  the scanner's exemption list in `scripts/secret-scan.sh` with the
  justification recorded in the same commit.

## Secret Rotation Procedure
1. **Generate** the new credential out-of-band (e.g. `openssl rand`). The new
   value must not appear in Git, reports, logs, chat, or tickets.
2. **Store** it only in the runtime secret store (container env / protected
   local secrets directory outside any repository).
3. **Deploy**: recreate the affected service with the new value; keep the
   image digest unchanged where possible.
4. **Invalidate**: rotate dependent session/signing secrets so old cookies and
   sessions cannot be replayed; verify old credentials return 401.
5. **Verify**: login, logout, TTL, allowlist, health, backing stores, and one
   rollback rehearsal against the recorded pre-rotation snapshot.
6. **Record** the rotation (what/when/window) without the secret value.

## Incident Reporting
- Report security issues via GitHub **Security Advisories**
  ("Report a vulnerability" on this repo's Security tab), or privately to the
  maintainers. Do not open public issues for suspected credential exposure.
- Include: affected component, suspected exposure window, refs/commits
  involved (if known). Do not include the credential value itself.
- Maintainers follow the rotation procedure above and record a post-incident
  summary (timeline, scope, remediation) in the internal incident record.

## Pre-Release Scan Requirements
Before publishing any release or tag:
1. `bash scripts/secret-scan.sh --path .` must pass on a clean checkout.
2. `bash scripts/secret-scan.sh --head <tag-ref>` must pass for the tag.
3. Every release asset must be scanned (text assets directly; binary assets by
   provenance timeline — assets built before a credential existed cannot
   contain it, and that provenance must be recorded).
4. The release body must not contain credentials or internal hostnames.
5. GHCR image tags must be immutable (no `latest`); image filesystem must be
   scanned for credential patterns before tagging a release.

## Known Issues
| ID | Description | Severity | Status |
|---|---|---|---|
| FG-FB-01 | Staging password in test scripts | P3 | RESOLVED 2026-09-26: password removed from scripts (env-var only), credential rotated, history remediation assessed (Plan A adopted: rotation + tree cleanup, no history rewrite) |
