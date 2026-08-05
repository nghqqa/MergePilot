#!/usr/bin/env bash
# MergePilot test-environment guard — fail-closed (v2).
#
# Every test inner/direct runner MUST source this at its top, before ANY
# docker build/run/rm/network/volume/pull command. It proves the script is
# running inside the isolated MergePilot-Test WSL2 distro with its dedicated,
# labeled test Docker daemon, and exits 2 BEFORE any docker operation if not —
# so M4-F/M5 tests can NEVER touch the HiClaw production daemon in
# Ubuntu-22.04.
#
# Source: source "${ROOT}/tools/test-env/mp_guard.sh"
#
# Phase 1 (identity) runs BEFORE any docker command and exits 2 immediately on
# any mismatch — this is the hard fail-closed guarantee. Phase 2 (daemon
# read-only checks) only runs once phase 1 has proven we are inside
# MergePilot-Test, and performs only `docker info`/`inspect`/`context` reads
# (never writes).

# ---------------------------------------------------------------------------
# Phase 1: identity — NO docker command may run before this block passes.
# ---------------------------------------------------------------------------
_mp_exit() {
  echo "MP_GUARD FAIL: $1" >&2
  echo "  Tests may ONLY run inside the MergePilot-Test WSL2 distro with its" >&2
  echo "  isolated Docker daemon. The Ubuntu-22.04 production daemon is" >&2
  echo "  FORBIDDEN for tests. No docker command was executed." >&2
  exit 2
}

if [ -z "${WSL_DISTRO_NAME:-}" ]; then
  _mp_exit "WSL_DISTRO_NAME is unset (not running inside any WSL distro)"
fi
if [ "${WSL_DISTRO_NAME}" != "MergePilot-Test" ]; then
  _mp_exit "WSL_DISTRO_NAME='${WSL_DISTRO_NAME}' (expected 'MergePilot-Test')"
fi
if [ "$(hostname 2>/dev/null)" != "mergpilot-test" ]; then
  _mp_exit "hostname='$(hostname 2>/dev/null)' (expected 'mergpilot-test')"
fi

# ---------------------------------------------------------------------------
# Phase 2: daemon read-only checks (MergePilot-Test proven; still no writes).
# ---------------------------------------------------------------------------

# DOCKER_HOST must be empty or point at one of the two known local unix sockets.
# Any other value (arbitrary unix:// path, tcp://, ssh://, fd://) is forbidden.
case "${DOCKER_HOST:-}" in
  ""|"unix:///var/run/docker.sock"|"unix:///run/docker.sock") : ;;
  *) _mp_exit "DOCKER_HOST='${DOCKER_HOST:-}' is not the local test socket (only empty / unix:///var/run/docker.sock / unix:///run/docker.sock allowed)" ;;
esac
case "${DOCKER_CONTEXT:-}" in
  ""|"default") : ;;
  *) _mp_exit "DOCKER_CONTEXT='${DOCKER_CONTEXT}' is not the default local context" ;;
esac

# docker reachable + carries the test-only label (set membership, not [0]).
_mp_labels="$(docker info --format '{{json .Labels}}' 2>/dev/null || true)"
if [ -z "$_mp_labels" ]; then
  _mp_exit "docker daemon unreachable (is dockerd running in MergePilot-Test?)"
fi
case "$_mp_labels" in
  *'"com.mergepilot.scope=test"'*) : ;;
  *) _mp_exit "daemon labels=$_mp_labels do not include 'com.mergepilot.scope=test' — this is NOT the test daemon" ;;
esac

# DockerRootDir must be the test daemon's own data root.
_mp_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [ "$_mp_root" != "/var/lib/docker" ]; then
  _mp_exit "DockerRootDir='${_mp_root}' (expected '/var/lib/docker' on the test vhdx)"
fi

# docker context host must be the local unix socket (no remote/prod endpoint).
_mp_ctx_host="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
case "$_mp_ctx_host" in
  unix:///var/run/docker.sock|unix:///run/docker.sock|"") : ;;
  *) _mp_exit "docker context host='${_mp_ctx_host}' is not the local unix socket" ;;
esac

# Forbidden production containers MUST NOT be visible from the test daemon,
# and no hiclaw-worker-* may exist in the test daemon's container store.
for _mp_c in mergepilot-controller policy-gw audit-pg github-mcp hiclaw-manager hiclaw-controller; do
  if docker inspect "$_mp_c" >/dev/null 2>&1; then
    _mp_exit "production container '${_mp_c}' is visible — this is NOT the isolated test daemon"
  fi
done
if [ "$(docker ps -a --filter 'name=hiclaw-worker-' --format '{{.Names}}' 2>/dev/null | grep -c . || true)" -gt 0 ]; then
  _mp_exit "hiclaw-worker-* containers exist in the test daemon — forbidden"
fi

echo "MP_GUARD OK: isolated MergePilot-Test daemon (label=com.mergepilot.scope=test, root=${_mp_root}, host=localhost)"
