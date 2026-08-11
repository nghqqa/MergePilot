# UPSTREAM_BLOCKED: HiClaw worker-creation hardening

## Status: PASSED — D2B-3 v1.2.2 production live verified (2026-08-12)

**Socket-proxy daemon IS implemented and deployed.** The MergePilot Docker
Socket Proxy (`tools/hiclab/docker_socket_proxy.py` + `proxy_transport.py`)
has been verified against the REAL AgentTeams v1.2.2 production dockerd
(Ubuntu-22.04) with genuine `agentteams-*` v1.2.2 images.

### Live evidence (authoritative)

- **hiclaw-v122-true-live-pass.json** — 64/64 PASS, `hiclaw_live=true`,
  `upstream_version=v1.2.2`, `upstream_source_commit=849182a`,
  `proxy_source_commit=e984ef3`. Real v1.2.2 worker/manager images used.
  This is the **current authoritative live evidence**.

- **hiclaw-v122-live-verify.json** (commit 6e90086) — 67/67 PASS with
  `hiclaw` name profile against v1.1.2 production images. Retained as
  **"v1.2.2 Proxy + v1.1.2 upstream compatibility live"** — NOT the true
  v1.2.2 upstream live evidence.

### What IS now hardened (D2B-3 complete)

  * **Socket proxy**: deny-by-default Unix reverse proxy. 13 SOURCE_PROVEN
    Docker API endpoints allowlisted; all others 403.
  * **Authoritative transform**: `restart=no`, tmpfs, StorageOpt, log limits,
    B5 strip-then-inject labels (scope/run_id/agent/hardened).
  * **Authoritative inspect**: every nameprefix op verifies Name + 4 labels
    exact-match before forwarding.
  * **Exec ID registry**: fail-closed; unknown/expired exec IDs denied.
  * **B11 archive path**: strict auth-token-dir allowlist.
  * **Unknown-role deny**: `agentteams-worker-evil` → DENY at classify.
  * **Marker lifecycle**: PID + config-digest binding; stale marker rejected.

### Manager auto-create

Manager auto-create is now **permitted** when the proxy is deployed and the
marker is valid. The proxy intercepts all container-create calls and enforces
the hardening policy. The `option b` operating restriction (Manager
auto-create FORBIDDEN) is **lifted** for deployments where the proxy is
running and the marker is valid.

### create_hardened_worker.sh

Retained as a **manual operator tool** for ad-hoc worker recreation outside
the proxy path. It is NOT the primary creation path when the proxy is
deployed (the proxy handles all creates).

### Previous status (historical, for audit trail)

Previously BLOCKED_UPSTREAM (option b): no socket-proxy daemon was
implemented; Manager auto-create was FORBIDDEN; only manual
`create_hardened_worker.sh` was permitted. That status was superseded by
the D2B-3 v1.2.2 production live pass on 2026-08-12.

### Programmatic enforcement (retained)

`guarded_start.py` PROGRAMMATICALLY refuses to start both
`agentteams-controller` (or `hiclaw-controller` in legacy deployments) AND
`agentteams-manager` when no valid capability marker is present.
`manager_start_allowed()` checks the marker files; the marker is written
only by the proxy's deploy step after self-check passes.

### D2B-3

D2B-3 is **runnable**. The proxy is implemented, deployed, and verified
against real v1.2.2 production images.

### disk guard + guarded startup (retained)

  * **disk_guard**: fail-closed on host+guest free space (retained).
  * **Guarded startup**: 6 managed containers restart=no; supervisor is
    the only start path, gated by disk_guard + phased health checks.
  * **cleanup tooling**: minio_cleanup.py + cleanup_run.py (DRY-RUN-ONLY
    candidates; --apply is fail-closed without authoritative probe).
