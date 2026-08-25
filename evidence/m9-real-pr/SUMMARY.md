# M9-A Dev-Machine Real-PR Replay — Summary

## What ran (all REAL, on this dev machine)

1. **PR#2 (Draft, never merged)**: nghqqa/MergePilot-Demo, head feat/download-endpoint
   - REV1 (0fee123): download() without realpath confinement (SEC-001 controlled defect)
   - REV2 (55e55f6): pipeline Fixer fix via isolated github-mcp-server (CAS + read-back)
   - Duplicate (a0e4e87b): pre-guard retry defect evidence (finding G, fixed)

2. **Pipeline chain** (all by the fixed MergePilot):
   - diff-parse: 5 files parsed from the real PR diff
   - risk-classify: **L2** (FILE_CONTENT_SURFACE + SOURCE_CONFIG_CHANGE) — was bare L1 before finding E fix
   - sast-scan: **1 high AST_PATH_TRAVERSAL** — was 0 findings before finding E fix
   - test-runner: REV1 1-failed → REV2 4/4-passed (on the real PR branch)
   - Fixer write: gh_fix_branch.py through mcporter → stdio github-mcp-server v1.9.0 (PAT isolated)

3. **Revision matrix** (9 cases): 8 PASS, 1 FAIL→fixed (finding G)
   - Protected branch / wrong repo / stale branch / path traversal / reader negatives all denied
   - Idempotent retry (post-guard) is a no-op

4. **Findings fixed this round**: A (checksums CRLF), B (pgvector 3-identity),
   C (WinNAT bind probe), D (death diagnostics), E (SAST+risk coverage),
   **F (pinned-server tool names — found live)**, **G (duplicate commit on retry — found live)**

## What did NOT run (honest limitations)

- The in-stack Workflow Controller and Policy Gateway containers (requires the full
  ISOLATED_LIVE stack; their decisions are enforced by the agent-team tools' own
  guard contracts in this replay — same rules, different enforcement point)
- No external physical machine acceptance (EXTERNAL_BLOCKED unchanged)
- PR#1 preserved as review-machine historical evidence (never modified)

## Verdict inputs

- revision_producer_contract: **evaluated** (see §5 verdict)
- audit_producer_contract: **evaluated** (see §5 verdict)
- All other truth boundaries: UNCHANGED (false / NOT_VERIFIED / false)
