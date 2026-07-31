---
name: pr-lifecycle
description: Create or reconcile a fix/revert PR and execute ticket-gated merge/close through the MergePilot Policy Gateway. High-risk GitHub writes only; deploy-owned role/repo/run context.
---

# pr-lifecycle · controlled GitHub write lifecycle

PRLifecycle is the reusable M4-D write Skill. It reuses the M4-A common runtime
and **only** reaches GitHub through the M3 Policy Gateway. It never accepts or
uses a PAT, arbitrary MCP tool name/arguments, shell command, local git
workspace, caller-selected role, repository, base branch, head branch, Gateway
URL or token.

## Actions

- `ensure_fix_pr` — deterministic `fix/<run>-<hmac>` branch, one atomic
  `push_files` commit, then a PR. Replays reconcile existing state.
- `ensure_revert_pr` — derives changed files from the deploy-bound bad merge and
  parent, restores `modified`/`removed` files, verifies content, creates a draft
  revert PR. `added`/rename/binary/delete requirements fail closed in v1.
- `merge_pr` — coordinator-only, exact M3 approval ticket and Gateway TOCTOU.
- `close_pr` — coordinator-only, exact M3 approval ticket.

There is no raw `create_branch`, file-write, `delete_file`, tool passthrough,
comment or PR metadata-update action in v1.

## Deploy-owned trust boundary

`MERGEPILOT_PRL_GATEWAY_URL`, `MERGEPILOT_PRL_ROLE`,
`MERGEPILOT_PRL_TOKEN`, `MERGEPILOT_PRL_REPO`,
`MERGEPILOT_PRL_BASE_BRANCH`, `MERGEPILOT_PRL_RUN_ID`,
`MERGEPILOT_PRL_RISK_LEVEL`, `MERGEPILOT_PRL_EXPECTED_BASE_SHA`,
`MERGEPILOT_PRL_HMAC_KEY`, and (for revert)
`MERGEPILOT_PRL_REVERT_BAD_SHA` / `MERGEPILOT_PRL_REVERT_PARENT_SHA`.

One process has one fixed role token. Fixer instances cannot call L2 actions;
coordinator instances cannot create/write branches or PRs. Missing or malformed
trusted context is denied before network access.

## Invocation

```text
python -m skills.pr_lifecycle.run < request.envelope.json
```

The optional MCP adapter dependencies are pinned in `requirements.txt`. The
module imports them lazily so framework-neutral unit tests can inject an
in-memory adapter. The production adapter requires Python 3.10 or newer (the
pinned MCP SDK requirement). M4-A/B/C compatibility regression and the
framework-neutral core continue to run on the established Python 3.9
verification environment; production adapter/E2E verification uses a separate
Python 3.10+ environment.

## Idempotency

Branch names and an internal, visible PR-body marker use HMAC-SHA256 with the
deploy key. The marker is plain text because the production github-mcp
`pull_request_read` result removes HTML comments.
The marker contains no raw idempotency key or code digest and is not an
authorization credential. Replays verify branch SHA, exact changed-file set,
file contents, PR repo/base/head and marker. Unknown or ambiguous state is a
conflict; PRLifecycle never force-pushes or treats arbitrary 409/422 responses
as success.

## Status and side effects

Success and existing-equivalent state return `status=OK`. Runtime/policy errors
return `status=ERROR` using only the M4-A generic codes; a controlled
`PRL_*` subcode is placed in `message`. No write action uses `PARTIAL`.

`side_effects` declares actual `network_read`, `network_write`, and
`github_write` attempts. A pre-validation denial has none; a Gateway policy
denial does not claim a GitHub write; a write-time unknown outcome declares the
attempt and is not blindly retryable.

Full frozen design: `docs/M4-D-PRLifecycle设计冻结.md`.
