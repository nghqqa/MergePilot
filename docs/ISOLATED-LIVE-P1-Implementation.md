# ISOLATED_LIVE Phase 1 Implementation

**Status**: P1 implementation candidate — local review candidate, not pushed, not merged.
**Branch**: `feat/isolated-live-p1`
**Commit chain**: `28fdd85` → `50156ba` → `c874c6a` → (new commit)

This is a local implementation candidate for review. It has not been pushed to
the remote, is not on an open PR, and is not merged. Nothing here is tagged or
released. This document describes what the candidate actually does and — just
as importantly — what it deliberately does not do. It avoids overclaiming.

## What this is

`ISOLATED_LIVE` is a read-only mode for the demo console. It serves the same
HTML pages as REPLAY mode plus two additional localhost HTTP endpoints that
expose a live snapshot of a single local JSON bundle. (Phase 1-E update: the
eight pages now also carry a dynamic-refresh engine — see "Served pages"
below.)

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
- **IPv4-loopback-only read-only HTTP server**: blocks POST/PUT/PATCH/DELETE,
  binds `127.0.0.1`/`localhost` only. IPv6 loopback (`::1`) is NOT implemented.
- **Factory hardening**: `create_server`/`make_handler` reject non-loopback
  hosts (including `::1`, `::`, `0.0.0.0`, LAN IPs), unknown modes, and
  mode/poller misconfigurations with `ValueError`
- **Fixture/file snapshot polling**: `FileSnapshotSource` reads JSON snapshots
  at fixed intervals
- **DemoBundle schema + integrity validation**: every snapshot is validated
  (schema + `bundle_sha256`) before atomic replacement
- **Invalid snapshot preservation**: corrupt / integrity-invalid / wrong-mode
  snapshots never overwrite the last valid one
- **Atomic status view**: `LivePoller.get_view()` returns stats + snapshot +
  SHA + `source_kind`/`source_read_only` (sourced from the actual
  `SnapshotSource`, never hardcoded) in a single locked read
- **Windows source locality classification**: `classify_source_locality` uses
  `kernel32.GetDriveTypeW` to classify the source drive; only `DRIVE_FIXED`
  yields a passing `VERIFIED_LOCAL` (see schema below)
- **Structured preflight result**: full field set including
  `source_locality_status`, `source_drive_type`, `source_drive_type_code`,
  and `source_locality_measurement_status` (see schema below)
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
- **IPv6 loopback (`::1`)** — not implemented. The P1 server is IPv4-loopback
  only (`127.0.0.1`/`localhost`); `::1` is rejected by preflight and by
  `create_server`.
- **Multi-tenant support** — none.
- **M8** — not defined. No M8 tag or release exists.

## Changed files

| File | Purpose |
|------|---------|
| `tools/demo_console/preflight.py` | Fail-closed preflight, mode isolation, IPv4-loopback-only, `classify_source_locality` (Win32 `GetDriveTypeW`), `source_locality_status` |
| `tools/demo_console/live_poller.py` | `SnapshotSource` (`read_only` property), `FileSnapshotSource`, `LivePoller` (mode isolation, `get_view` with dynamic `source_kind`/`source_read_only`) |
| `tools/demo_console/serve.py` | IPv4-loopback-only server factory hardening, dynamic `source_kind`/`source_read_only` in status contract, shutdown timeout |
| `tools/demo_console/schema.py` | `validate_bundle(expected_mode=...)` mode enforcement |
| `tools/demo_console/integrity.py` | Canonical JSON + `bundle_sha256` (single source of truth) |
| `tests/demo_console/test_isolated_live.py` | Real-HTTP integration tests + new mode/status/factory/shutdown/locality/IPv4/dynamic-kind/doc suites |
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

## Network binding

The P1 demo server is **IPv4-loopback only**. Only `127.0.0.1` and `localhost`
are accepted bind hosts, enforced identically in preflight and in
`create_server`:

- `127.0.0.1` — accepted
- `localhost` — accepted
- `::1` (IPv6 loopback) — **rejected**. The P1 server is IPv4-loopback only;
  IPv6 `::1` is not implemented. Preflight surfaces
  `"P1 server is IPv4-loopback only; IPv6 ::1 not implemented"`.
- `::`, `0.0.0.0`, LAN IPs (e.g. `192.168.1.1`) — **rejected**. The console
  never binds off-machine.

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

## Source locality (Windows drive classification)

`classify_source_locality(path)` determines whether a snapshot source path
backs onto a local volume, using `kernel32.GetDriveTypeW` on Windows. The
classification is **fail-closed**: only `VERIFIED_LOCAL` yields
`source_is_local_file=true` and a passing preflight.

Win32 drive-type codes and their handling:

| Code | Constant | Status | Passes preflight? |
|------|----------|--------|-------------------|
| 0 | `DRIVE_UNKNOWN` | `NOT_MEASURED` | no |
| 1 | `DRIVE_NO_ROOT_DIR` | `NOT_MEASURED` | no |
| 2 | `DRIVE_REMOVABLE` | `UNSUPPORTED_DRIVE_TYPE` | no |
| 3 | `DRIVE_FIXED` | `VERIFIED_LOCAL` | **yes** |
| 4 | `DRIVE_REMOTE` | `NETWORK_PATH_REJECTED` (`NETWORK_DRIVE_REJECTED`) | no |
| 5 | `DRIVE_CDROM` | `UNSUPPORTED_DRIVE_TYPE` | no |
| 6 | `DRIVE_RAMDISK` | `UNSUPPORTED_DRIVE_TYPE` | no |

Additional rules:

- UNC paths (`\\server\share` or `//server/share`) → `NETWORK_PATH_REJECTED`.
- `http(s)://` URLs and `file://` URIs → `NETWORK_PATH_REJECTED`.
- If the Win32 API raises (`OSError`) → `NOT_MEASURED` (fail-closed).
- POSIX (non-Windows): a regular file is classified `POSIX_LOCAL_CANDIDATE`
  with failure `POSIX_LOCALITY_NOT_VERIFIED`, mapped to `NOT_MEASURED` →
  fail-closed. There is no portable Win32-style drive-type check off Windows.

Fail-closed invariants:

- `NOT_MEASURED` **never** coexists with `preflight_passed=true`.
- `source_is_local_file=true` **only** when `status == VERIFIED_LOCAL`.

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
  "source_path_resolved": "C:\\abs\\path\\to\\snapshot.json",
  "source_locality_status": "VERIFIED_LOCAL",
  "source_drive_type": "DRIVE_FIXED",
  "source_drive_type_code": 3,
  "source_locality_measurement_status": "MEASURED",
  "browser_network_observation_status": "NOT_MEASURED",
  "observed_external_network_requests": null,
  "checked_at": "2026-08-13T...",
  "failures": []
}
```

`source_locality_status` values:

- `VERIFIED_LOCAL` — a regular local file on a `DRIVE_FIXED` volume. The only
  status that sets `source_is_local_file=true` and passes preflight.
- `NETWORK_PATH_REJECTED` — a UNC path (`\\server\share` / `//server/share`),
  a mapped network drive (`DRIVE_REMOTE`), a `file://` URI, or an
  `http(s)://` URL. These are refused.
- `UNSUPPORTED_DRIVE_TYPE` — `DRIVE_REMOVABLE`, `DRIVE_CDROM`, or
  `DRIVE_RAMDISK`. Refused.
- `POSIX_LOCAL_CANDIDATE` — a POSIX path that exists as a regular file, but
  whose backing store cannot be Win32-verified. Fail-closed to
  `NOT_MEASURED`.
- `NOT_MEASURED` — `DRIVE_UNKNOWN`, `DRIVE_NO_ROOT_DIR`, a Win32 API failure,
  or the POSIX fail-closed mapping. **Never** coexists with
  `preflight_passed=true`.

`source_locality_measurement_status` is `MEASURED` only when the source was
successfully classified as `VERIFIED_LOCAL`; otherwise `NOT_MEASURED`.

## Status API contract (`GET /api/live/status`)

The status endpoint returns the full browser-observable contract, read in a
single atomic snapshot via `poller.get_view()`. `source_kind` and
`source_read_only` come from the **actual** `SnapshotSource` (via
`view.get("source_kind", "UNKNOWN")` / `view.get("source_read_only", True)`),
not a hardcoded constant — so a future source type reports its own kind.

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
  "dynamic_pages_consume_live_api": true
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
- `dynamic_pages_consume_live_api=true` (Phase 1-E): the served pages carry
  `live-refresh.js`, a dynamic-refresh engine that GETs only
  `/api/live/status` and `/api/live/snapshot`, re-renders all 8 views from
  ONE shared snapshot, clamps its interval to >= 2000 ms, stops after 10
  consecutive failures (manual refresh restarts), and never falls back to
  REPLAY/static/baked data. In REPLAY mode the live endpoints 404 and the
  engine never starts — the frozen HTML stays exactly as served. serve.py
  fail-closed-verifies the shipped JS against the Python contract
  (`tools/demo_console/live_refresh.py`) at startup.
- `bundle_sha256` equals the bundle's internal `bundle_sha256` field.
- `source_snapshot_sha256` equals the SHA-256 of the raw snapshot bytes read
  from disk.
- `source_kind` is read from `SnapshotSource.kind` (e.g. `FILE_FIXTURE`); a
  custom source reports its own kind.
- `source_read_only` is read from `SnapshotSource.read_only` (default `True`).

## Served pages

The console serves 8 HTML pages from `samples/demo-console/`
(`overview`, `timeline`, `findings`, `rag`, `trace`, `safety`, `evidence`,
`benchmark`).

Phase 1-E update — dynamic refresh: in ISOLATED_LIVE mode all 8 pages are
re-rendered from the ONE shared live snapshot (GET `/api/live/snapshot`),
with a freshness banner (data time, poll state, failure count) and a manual
refresh button. On engine start the baked REPLAY payload is immediately
replaced by placeholders — baked content is never displayed as live data.
Refresh failures keep the last LIVE data and mark it stale; there is NO
fallback to REPLAY, static, or fabricated data. In REPLAY mode the engine
never starts (the live endpoints 404) and the frozen HTML stays as served.

## Boundaries

- Mode is fixed at startup; cannot be switched at runtime.
- ISOLATED_LIVE requires `--source-file`; missing source → exit 1.
- `http(s)://`, `file://`, and UNC/network source paths are forbidden.
- Bind host must be IPv4 loopback (`127.0.0.1`/`localhost`); `::1`, `::`,
  `0.0.0.0`, and LAN IPs are rejected (preflight and `create_server`).
- Source must back onto a `DRIVE_FIXED` local volume (`VERIFIED_LOCAL`).
  Removable/CD/RAM drives are unsupported; mapped network drives
  (`DRIVE_REMOTE`) are rejected; an unclassifiable drive (`DRIVE_UNKNOWN` /
  API failure) is fail-closed (`NOT_MEASURED` → preflight fails).
- Unknown modes raise `ValueError` (no silent REPLAY fallback).
- Write HTTP methods return 405.
- `runtime_consumes_rag_context=false` always.
- `github_writes_enabled=false` always.
- `agent_control_enabled=false` always.
- `production_resource_accessed=null` / `production_resource_access_status=NOT_MEASURED`.
- `dynamic_pages_consume_live_api=true` in live mode (Phase 1-E; the
  REPLAY-mode pages remain frozen static HTML).
