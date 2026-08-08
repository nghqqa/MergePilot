#!/bin/bash
# Runtime nc shim for the HiClaw manager image, which ships WITHOUT netcat.
# The official waitForService() (lib/base.sh) falls back to `nc -z host port`
# when `curl -sf http://host:port/` fails (e.g. MinIO root returns 403). Without
# nc, MinIO readiness loops until timeout and the manager FATALs. This shim is
# bind-mounted at /usr/bin/nc (read-only) — it does NOT modify the official
# image or entrypoint; it provides the missing binary the script already expects.
# Only the `-z` (zero-IO TCP probe) form is implemented; other invocations pass
# through with a best-effort /dev/tcp connect.
set -u
z=0; host=""; port=""
while [ $# -gt 0 ]; do
  case "$1" in
    -z) z=1; shift ;;
    -w*) shift ;;            # skip timeout arg (-wN or -w N handled by shift)
    -*) shift ;;
    *) [ -z "$host" ] && host="$1" || port="$1"; shift ;;
  esac
done
if [ "$z" = 1 ] && [ -n "$host" ] && [ -n "$port" ]; then
  timeout 3 bash -c "exec 3<>\/dev\/tcp/${host}/${port}" 2>/dev/null && exit 0 || exit 1
fi
exit 1
