# API Contracts

## GET /api/pulls
- Auth: session required (401 without)
- Query: `?repo=owner/name` (optional, 403 if not in allowlist)
- Response: `{pulls: [{repo, pr_number, head_sha, run_id}], source}`
- source enum: POSTGRESQL_LIVE | BACKEND_NOT_WIRED | BACKEND_ERROR

## GET /api/pending
- Auth: session required
- Response: `{pending: [{ticket_id, repo, pr_number, head_sha, action, status, approval_expires_at}], source}`

## GET /api/tickets
- Auth: session required
- Response: `{tickets: [{ticket_id, run_id, repo, pr_number, action, status, is_expired, ...}], source}`

## GET /api/evidence
- Auth: session required
- Response: `{evidence: [{skill_name, status, binding_status, integrity, run_id, head_sha, ...}], source}`

## GET /api/audit
- Auth: session required
- Response: `{gate_decisions: [{run_id, decision, created_at}], core_source}`

## GET /api/overview
- Auth: session required
- Response: `{schema_version, generated_at, source, stage_counts, repository_counts, trend, pending_summary, incidents, health, prs}`

## GET /api/auth/session
- Response 200: `{user: {name}, expires_at, repos}` 
- Response 401: `{error: {reason: "not_authenticated"}}`

## POST /api/auth/login
- Body: `{user, password}`
- Response 200: Set-Cookie mp_session + mp_csrf
- Response 401: invalid credentials
- Response 503: auth not configured

## POST /api/auth/logout
- Headers: `X-CSRF-Token` required
- Response 200: session invalidated

## GET /api/rag/org-search
- Auth: session required
- A-chain OFF: `{service_state: "a_chain_disabled"}`
- A-chain ON + hit: `{service_state: "ok", results, snapshot_id, corpus_digest, retrieval_version, source_refs, knowledge_type: "org_knowledge"}`
- A-chain ON + degraded: 503, `{service_state: "degraded", degraded_reason}`
