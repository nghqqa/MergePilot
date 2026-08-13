# ISOLATED_LIVE Phase 1 Implementation

**Status**: P1 implementation candidate — local review candidate, not pushed, not merged.
**Branch**: `feat/isolated-live-p1`
**Commit chain**: `28fdd85` → `50156ba` → (new commit)

This is a local implementation candidate for review. It has not been pushed to
the remote, is not on an open PR, and is not merged. Nothing here is tagged or
released. This document describes what the candidate actually does and — just
as importantly — what it deliberately does not do. It avoids overclaiming.

## What this is

`ISOLATED_LIVE` is a read-only mode for the demo console. It serves the same
static, frozen REPLAY HTML pages as REPLAY mode, plus two additional localhost
HTTP endpoints that expose a live snapshot of a single local JSON bundle:

- `GET /api/live/snapshot` — the current valid bundle JSON (503 if none)
- `GET /api/live/status` — structured poller/status JSON (full contract below)

The "live" snapshot is read from a local file at a fixed interval, validated
against the DemoBundle schema, and atomically swapped in. Invalid snapshots
never overwrite the last valid one.

## Implemented

- **Explicit mode selection**: `replay` (default) and `isolated_live`
- **Mode isolation**: a poller/preflight configured for one mode rejects the
  other mode's bundles (`demo_mode` mismatch → `MODE_MISMATCH`)
- **Fail-closed preflight**: all checks must pass before the server starts
- **Loopback-only read-only HTTP server**: blocks POST/PUT/PATCH/DELETE,
  binds `127.0.0.1`/`localhost`/`::1` only
- **Factory hardening**: `create_server`/`make_handler` reject non-loopback
  hosts, unknown modes, and mode/poller misconfigurations with `ValueError`
- **Fixture/file snapshot polling**: `FileSnapshotSource` reads JSON snapshots
  at fixed intervals
- **DemoBundle schema + integrity validation**: every snapshot is validated
  (schema + `bundle_sha256`) before atomic replacement
- **Invalid snapshot preservation**: corrupt / integrity-invalid / wrong-mode
  snapshots never overwrite the last valid one
- **Atomic status view**: `LivePoller.get_view()` returns stats + snapshot +
  SHA in a single locked read
- **Structured preflight result**: full field set including
  `source_locality_status` (see schema below)
- **Poller statistics**: `poll_count`, `last_poll_at`, `last_success_at`,
  `source_snapshot_sha256`, `consecutive_failures`, `last_error_code`, `state`
- **State machine**: `INIT` → `LIVE` → `STALE` (on transient failure) →
  `DEGRADED` (on threshold) → `STOPPED`
- **Shutdown reporting**: a poller that does not stop within the grace period
  is reported as `POLLER_SHUTDOWN_TIMEOUT` and the process exits non-zero

## NOT implemented

- **PostgreSQL live source** — not implemented. The `SnapshotSource` interface
  is frozen for it, but no database driver or connection path exists.
- **MergePilot-Test formal integration** — not integrated.
- **Production database access** — none. The console does not touch any
  production datastore.
- **Production management dashboard** — none. There is no management UI.
- **Write / control operations** — none. No GitHub writes, no agent lifecycle
  control, no merge/rollback control. All write HTTP methods return 405.
- **External LLM API calls** — none.
- **Multi-tenant support** — none.
- **M8** — not defined. No M8 tag or release exists.

## Changed files

| File | Purpose |
|------|---------|
| `tools/demo_console/preflight.py` | Fail-closed preflight, mode isolation, `source_locality_status` |
| `tools/demo_console/live_poller.py` | `SnapshotSource`, `FileSnapshotSource`, `LivePoller` (mode isolation, `get_view`) |
| `tools/demo_console/serve.py` | Server factory hardening, full status contract, shutdown timeout |
| `tools/demo_console/schema.py` | `validate_bundle(expected_mode=...)` mode enforcement |
| `tools/demo_console/integrity.py` | Canonical JSON + `bundle_sha256` (single source of truth) |
| `tests/demo_console/test_isolated_live.py` | Real-HTTP integration tests + new mode/status/factory/shutdown/locality/doc suites |
| `docs/ISOLATED-LIVE-P1-Implementation.md` | This document |

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

## Bundle mode / integrity contract

A DemoBundle declares its mode in `demo_mode` (one of `REPLAY`,
`ISOLATED_LIVE`). Mode isolation is enforced at every boundary:

- **`validate_bundle(bundle, expected_mode=...)`** rejects a bundle whose
  `demo_mode` does not match the expected mode. A REPLAY bundle is rejected
  in an `ISOLATED_LIVE` context and vice versa.
- **Preflight** validates an `isolated_live` source with
  `expected_mode="ISOLATED_LIVE"`.
- **The poller** validates each snapshot with
  `expected_mode=self._expected_mode` (default `ISOLATED_LIVE`). A mismatch
  yields the stable error code `MODE_MISMATCH` (not a generic `ValueError`),
  preserves the last valid snapshot, and transitions to `STALE` (or
  `DEGRADED` past the threshold).

Bundle integrity: `bundle_sha256` is the SHA-256 of the canonical JSON of the
bundle **excluding** volatile fields (`bundle_sha256`, `generated_at`).
`verify_bundle_integrity` recomputes it and rejects any mismatch. This is the
single authoritative digest, shared by the builder, schema, preflight, and
poller.

## Preflight result schema

```json
{
  "mode": "ISOLATED_LIVE",
  "preflight_passed": true,
  "source_kind": "FILE_FIXTURE",
  "source_read_only": true,
  "loopback_only": true,
  "production_resource_accessed": null,
  "production_resource_access_status": "NOT_MEASURED",
  "external_network_required": false,
  "github_writes_enabled": false,
  "agent_control_enabled": false,
  "runtime_consumes_rag_context": false,
  "source_path_kind": "LOCAL_FILE",
  "source_is_local_file": true,
  "source_is_network_path": false,
  "source_path_resolved": "/abs/path/to/snapshot.json",
  "source_locality_status": "VERIFIED_LOCAL",
  "browser_network_observation_status": "NOT_MEASURED",
  "observed_external_network_requests": null,
  "checked_at": "2026-08-13T...",
  "failures": [],
  "source_locality_limitation": "Windows mapped-drive sources are classified NOT_MEASURED ..."
}
```

`source_locality_status` values:

- `VERIFIED_LOCAL` — a regular local filesystem path whose source is a local
  file (non-drive-letter path on a local volume).
- `NETWORK_PATH_REJECTED` — a UNC path (`\\server\share` / `//server/share`),
  a `file://` URI, or an `http(s)://` URL. These are refused.
- `NOT_MEASURED` — a Windows mapped drive (e.g. `D:\`). The console cannot
  portably determine whether a drive letter backs onto a local volume or a
  network share, so it does not fail-closed on every drive-letter path. Such
  sources are allowed but their locality is explicitly unverified. The
  `source_locality_limitation` field documents this.

## Status API contract (`GET /api/live/status`)

The status endpoint returns the full browser-observable contract, read in a
single atomic snapshot via `poller.get_view()`:

```json
{
  "mode": "ISOLATED_LIVE",
  "source_kind": "FILE_FIXTURE",
  "source_read_only": true,
  "not_production": true,
  "poller_state": "LIVE",
  "poll_count": 42,
  "last_poll_at": "2026-08-13T...",
  "last_success_at": "2026-08-13T...",
  "source_snapshot_sha256": "<sha256 of raw snapshot bytes>",
  "bundle_sha256": "<bundle's internal bundle_sha256>",
  "consecutive_failures": 0,
  "last_error_code": "",
  "github_writes_enabled": false,
  "agent_control_enabled": false,
  "runtime_consumes_rag_context": false,
  "production_resource_accessed": null,
  "production_resource_access_status": "NOT_MEASURED",
  "browser_network_observation_status": "NOT_MEASURED",
  "observed_external_network_requests": null,
  "dynamic_pages_consume_live_api": false
}
```

Field semantics:

- `production_resource_accessed` is `null`, **not `false`**. The console does
  not measure production access — it only refuses it. The companion
  `production_resource_access_status` is `NOT_MEASURED` so the absence of
  measurement is explicit and cannot be mistaken for a measured "clean".
- `browser_network_observation_status` is `NOT_MEASURED` and
  `observed_external_network_requests` is `null`: the console does not
  instrument outbound browser traffic.
- `dynamic_pages_consume_live_api=false`: the served pages are static, frozen
  REPLAY HTML. There are a fixed set of 8 static pages and they are **not
  dynamically refreshed** — they do not poll or consume the live API at
  runtime. The live API exists only for an operator who explicitly requests
  `/api/live/*`.
- `bundle_sha256` equals the bundle's internal `bundle_sha256` field.
- `source_snapshot_sha256` equals the SHA-256 of the raw snapshot bytes read
  from disk.

## Served pages

The console serves a static, frozen set of 8 REPLAY HTML pages from
`samples/demo-console/`. These pages are pre-rendered and are **not**
dynamically refreshed. They do not consume the live API. The ISOLATED_LIVE
endpoints are additive: they expose the live snapshot/status for an operator,
but the user-facing pages themselves remain static REPLAY HTML.

## Boundaries

- Mode is fixed at startup; cannot be switched at runtime.
- ISOLATED_LIVE requires `--source-file`; missing source → exit 1.
- `http(s)://`, `file://`, and UNC/network source paths are forbidden.
- Non-loopback hosts are rejected (preflight and `create_server`).
- Unknown modes raise `ValueError` (no silent REPLAY fallback).
- Write HTTP methods return 405.
- `runtime_consumes_rag_context=false` always.
- `github_writes_enabled=false` always.
- `agent_control_enabled=false` always.
- `production_resource_accessed=null` / `production_resource_access_status=NOT_MEASURED`.
- `dynamic_pages_consume_live_api=false` always.
