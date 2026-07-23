#!/bin/bash
# Leave + forget the given room IDs (as admin). Run inside hiclaw-manager.
HS=http://hiclaw-controller:6167
PASS="$1"; shift
TOK=$(curl -sf -X POST "$HS/_matrix/client/v3/login" -H "Content-Type: application/json" \
  -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"admin\"},\"password\":\"$PASS\"}" | jq -r .access_token)
if [ -z "$TOK" ] || [ "$TOK" = "null" ]; then echo "LOGIN FAILED"; exit 1; fi
echo "admin token ok, deleting $# room(s):"
for R in "$@"; do
  printf "  %s : " "$R"
  LEAVE=$(curl -s -X POST -H "Authorization: Bearer $TOK" "$HS/_matrix/client/v3/rooms/$R/leave" -d "{}")
  FORGET=$(curl -s -X POST -H "Authorization: Bearer $TOK" "$HS/_matrix/client/v3/rooms/$R/forget" -d "{}")
  ERR=$(echo "$LEAVE $FORGET" | jq -r '.errcode // empty' 2>/dev/null | head -1)
  if [ -n "$ERR" ]; then echo "leave/forget -> $ERR"; else echo "left + forgotten"; fi
done
