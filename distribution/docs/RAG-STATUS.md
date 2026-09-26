# RAG Status

## Current State
```
A-chain (org knowledge lexical) = OFF (a_chain_disabled)
C-chain (skill_case_retrieval) = BLOCKED
embedding = DISABLED
pgvector = DISABLED
model_cache = DISABLED
RUN_BINDING_AUTH = NOT_WIRED
```

## A-Chain (Organization Knowledge Lexical Retrieval)
Status: Implemented, verified, CLOSED.
- Version: lexical-zh-en-v1 (lexical, no embedding)
- Corpus: org security standards (content-addressed, snapshot_id + SHA256)
- Audit: five fields per retrieval
- Degraded: 503 + explicit marker (never masquerades as success)
- Enable: requires human authorization (MERGEPILOT_ORG_RAG_A_CHAIN=1)
- Evidence: preflight 11/11, CI 12/12, staging 9/9, LUO 10/10, pilot 10/10

## C-Chain (skill_case_retrieval)
Status: BLOCKED (3 gaps, all require operator action).

### Gap 1: Approved model cache
- Requires: operator offline model acquisition + SHA256 manifest + signed approval
- Template: included in deployment package (docs/secrets/)

### Gap 2: Provider metadata attestation
- Requires: case_provider_metadata table + live contract tests + tests_attested=true
- Migration ready (applied on isolated PG)

### Gap 3: RUN_BINDING_AUTH key distribution
- Requires: key generation/distribution/rotation/revocation mechanism
- Contract tests passed (26/26)

### Prohibited
- No model downloads (D7 single-authorization is only channel)
- No embedding generation
- No pgvector
- No provider registration
- No RUN_BINDING_AUTH bypass

## Semantic Red Lines
- A-chain results are org-standard reference only, NOT risk decisions
- Retrieval does NOT create findings or alter gate/stage/ticket/success
- Degraded NEVER displayed as normal success
