# Limitations

## Console
- Single-instance session store (no horizontal scaling)
- No TLS termination (requires reverse proxy)
- Stage derivation depends on PG data

## RAG
- A-chain: implemented but default OFF
- C-chain: BLOCKED (3 gaps, see RAG-STATUS.md)
- embedding: not enabled
- pgvector: not used
- RUN_BINDING_AUTH: NOT_WIRED

## Fixer/Verifier
- Only verified in isolated fixture
- Cannot auto-process real PRs
- Production containers not started
- Requires model client (budget guard)

## GitHub
- Read-only (zero writes)
- No approve/reject/push/merge support

## Deployment
- Docker Compose only (no Kubernetes)
- Image local-only (no registry push)
- No CI/CD pipeline

## Known Issues
| ID | Description | Severity |
|---|---|---|
| FG-FB-01 | Staging password in test scripts | P3 |
