#!/bin/bash
# C1 agent wrapper (manager + worker). Reads secrets from /secrets (read-only
# bind mount), exports them, then `exec`s the official HiClaw entrypoint ($@)
# so it becomes PID 1 (preserving the long-running process model). Stdout+stderr
# are routed through an async sed filter (process substitution) that redacts
# Matrix access_token values — so docker logs never contain the real token,
# while preserving login markers + the entrypoint's own exit behavior.
set -uo pipefail
for v in HICLAW_REGISTRATION_TOKEN HICLAW_FS_SECRET_KEY; do
  f="/secrets/${v}"
  if [ -r "$f" ]; then
    export "${v}=$(cat "$f")"
  fi
done
# exec replaces this shell with the entrypoint (PID 1). Process substitution
# > >(...) filters stdout+stderr in real time (-u = unbuffered). sed's own
# stdout inherits the container's stdout → docker logs capture redacted output.
# Two patterns: JSON ("access_token":"...") + key=value (access_token=... / access_token:...).
exec "$@" > >(sed -u -E \
  -e 's/("access_token"[[:space:]]*:[[:space:]]*")[^"]*"/\1<redacted-m5c>"/g' \
  -e 's/(access_token[=:][[:space:]]*)[^,[:space:]}"]+/\1<redacted-m5c>/g') 2>&1
