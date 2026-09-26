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

## Known Issues
| ID | Description | Severity |
|---|---|---|
| FG-FB-01 | Staging password in test scripts | P3 |
