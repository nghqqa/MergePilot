#!/bin/bash
# List all rooms the admin has joined, with name + members. Run inside hiclaw-manager.
HS=http://hiclaw-controller:6167
PASS="$1"
LOGIN=$(curl -sf -X POST "$HS/_matrix/client/v3/login" -H "Content-Type: application/json" \
  -d "{\"type\":\"m.login.password\",\"identifier\":{\"type\":\"m.id.user\",\"user\":\"admin\"},\"password\":\"$PASS\"}")
TOK=$(echo "$LOGIN" | jq -r .access_token)
if [ -z "$TOK" ] || [ "$TOK" = "null" ]; then
  echo "LOGIN FAILED: $LOGIN"; exit 1
fi
echo "admin token ok (len ${#TOK})"
echo ""
ROOMS=$(curl -sf -H "Authorization: Bearer $TOK" "$HS/_matrix/client/v3/joined_rooms" | jq -r ".joined_rooms[]")
echo "ROOM_ID | NAME | MEMBERS"
echo "--------|------|--------"
for R in $ROOMS; do
  NAME=$(curl -sf -H "Authorization: Bearer $TOK" "$HS/_matrix/client/v3/rooms/$R/state/m.room.name" 2>/dev/null | jq -r ".name // \"\"" 2>/dev/null)
  MEMBERS=$(curl -sf -H "Authorization: Bearer $TOK" "$HS/_matrix/client/v3/rooms/$R/joined_members" 2>/dev/null | jq -r ".joined | to_entries[] | (.value.display_name // .key)" 2>/dev/null | paste -sd, -)
  echo "$R | ${NAME:-(no name)} | $MEMBERS"
done
echo ""
echo "ADMIN_TOKEN=$TOK"
