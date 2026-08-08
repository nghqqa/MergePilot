#!/bin/sh
# C1 secret-file wrapper: runs as the embedded controller's entrypoint. Reads
# secret values from /secrets (read-only bind mount), exports them into the
# process environment, then execs the official supervisord entrypoint. This
# keeps secret values OUT of `docker run -e` / Config.Env — Config.Env carries
# only SECRETS_DIR=/secrets (a non-secret path). Caller pre-populates /secrets.
set -eu
for v in HICLAW_REGISTRATION_TOKEN HICLAW_MINIO_PASSWORD; do
  f="/secrets/${v}"
  if [ -r "$f" ]; then
    val="$(cat "$f")"
    export "$v=$val"
  fi
done
exec supervisord -n -c /etc/supervisor/supervisord.conf
