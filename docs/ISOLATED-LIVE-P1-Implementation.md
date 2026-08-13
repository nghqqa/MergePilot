# ISOLATED_LIVE Phase 1 Implementation

**Status**: Phase 1 complete (local commit, not pushed, not PR'd)
**Branch**: `feat/isolated-live-p1`
**Base**: `c76cd75` (origin/main)

## Implemented

- **Explicit mode selection**: `replay` (default) and `isolated_live`
- **Fail-closed preflight**: all checks must pass before server starts
- **Loopback-only read-only HTTP server**: blocks POST/PUT/PATCH/DELETE, binds 127.0.0.1
- **Fixture/file snapshot polling**: `FileSnapshotSource` reads JSON snapshots at fixed intervals
- **DemoBundle schema validation**: every snapshot validated before atomic replacement
- **Invalid snapshot preservation**: corrupt/invalid snapshots never overwrite the last valid one
- **Structured preflight result**: 12-field JSON with independent failure recording
- **Poller statistics**: poll_count, last_poll_at, last_success_at, sha256, consecutive_failures, state
- **State machine**: INIT → LIVE → STALE (on transient failure) → DEGRADED (on threshold) → STOPPED

## NOT Implemented

- PostgreSQL live source (contract frozen in `SnapshotSource` interface; no heavy deps added)
- MergePilot-Test formal integration
- Production database access
- Production management dashboard
- GitHub write operations
- Agent lifecycle control
- Merge / rollback control
- Production Docker / HiClaw / real cloud SLS
- External LLM API calls
- Multi-tenant support
- M8 tag or Release

## CLI

```bash
# REPLAY (default, unchanged):
python tools/demo_console/serve.py --port 8080

# ISOLATED_LIVE:
python tools/demo_console/serve.py \
    --mode isolated_live \
    --source-file /path/to/snapshot.json \
    --host 127.0.0.1 \
    --port 8080 \
    --poll-interval 2
```

## Preflight Result Schema

```json
{
  "mode": "ISOLATED_LIVE",
  "preflight_passed": true,
  "source_kind": "FILE_FIXTURE",
  "source_read_only": true,
  "loopback_only": true,
  "production_resource_accessed": false,
  "external_network_required": false,
  "github_writes_enabled": false,
  "agent_control_enabled": false,
  "runtime_consumes_rag_context": false,
  "checked_at": "2026-08-13T...",
  "failures": []
}
```

## Files

| File | Purpose |
|------|---------|
| `tools/demo_console/preflight.py` | Preflight checks + result schema |
| `tools/demo_console/live_poller.py` | SnapshotSource interface, FileSnapshotSource, LivePoller thread |
| `tools/demo_console/serve.py` | Extended with --mode, --source-file, --poll-interval |
| `tests/demo_console/test_isolated_live.py` | 22+ test cases |
| `docs/ISOLATED-LIVE-P1-Implementation.md` | This document |

## Boundaries

- Mode is fixed at startup; cannot be switched at runtime
- ISOLATED_LIVE requires `--source-file`; missing source → exit 1
- http/https source URLs forbidden
- Non-loopback hosts rejected
- Write HTTP methods return 405
- `runtime_consumes_rag_context=false` always
- `github_writes_enabled=false` always
- `agent_control_enabled=false` always
- `production_resource_accessed=false` always
