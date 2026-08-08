#!/bin/sh
# C1 Candidate secret-file wrapper. Reads secret values from /secrets (read-only
# bind mount), exports them into the process environment, then execs the official
# Candidate entrypoint (python3 -u controller.py). Keeps PG_PASS / MATRIX_PASS /
# ADMIN_PW / GATEWAY_TOKEN out of `docker -e` / Config.Env.
set -eu
for v in MATRIX_PASS PG_PASS ADMIN_PW GATEWAY_TOKEN HICLAW_REGISTRATION_TOKEN; do
  f="/secrets/${v}"
  if [ -r "$f" ]; then
    val="$(cat "$f")"
    export "$v=$val"
  fi
done
exec python3 -u controller.py
