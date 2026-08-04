#!/usr/bin/env bash
# Inner: runs inside WSL where docker is available. Plain bash, no WSL wrapping.
set -euo pipefail
LOCK_LABEL="mergepilot:m5-0-candidate"
UNIQ="m5al-$$-$(date +%s)"
CONTAINER="m5itest-pg-${UNIQ}"
PG_PW="$(head -c 32 /dev/urandom | od -An -v -tx1 | tr -d ' \n')"
DBNAME="m5test"

cleanup() { set +e; docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "=== M5-0A advisory lock integration (real PG16, throwaway) ==="
echo "container=$CONTAINER label='$LOCK_LABEL'"

docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD="$PG_PW" -e POSTGRES_DB="$DBNAME" \
  pgvector/pgvector:pg16 >/dev/null

for i in $(seq 1 30); do
  docker exec "$CONTAINER" pg_isready -U postgres -d "$DBNAME" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$CONTAINER" pg_isready -U postgres -d "$DBNAME" >/dev/null 2>&1 \
  || { echo "FAIL: PG not ready"; exit 1; }
echo "PG16 ready"

# Acquire lock in background session (holds for 45s via pg_sleep)
docker exec -d "$CONTAINER" psql -U postgres -d "$DBNAME" \
  -c "SELECT pg_try_advisory_lock(hashtextextended('$LOCK_LABEL', 0))" \
  -c "SELECT pg_sleep(45)" \
  -c "SELECT pg_advisory_unlock(hashtextextended('$LOCK_LABEL', 0))" >/dev/null
sleep 2

# Gate 1: second session DENIED
G1=$(docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_try_advisory_lock(hashtextextended('$LOCK_LABEL', 0))")
[ "$G1" = "f" ] || { echo "Gate 1 FAIL: session 2 should be DENIED (got $G1)"; exit 1; }
# release our test attempt (does NOT affect session 1's session-scoped lock)
docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_advisory_unlock(hashtextextended('$LOCK_LABEL', 0))" >/dev/null
echo "Gate 1 PASS: session 2 DENIED while session 1 holds (f)"

# Gate 2: session 1 still holds (session-scoped; our unlock didn't affect it)
G2=$(docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_try_advisory_lock(hashtextextended('$LOCK_LABEL', 0))")
[ "$G2" = "f" ] || { echo "Gate 2 FAIL: lock should still be held (got $G2)"; exit 1; }
docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_advisory_unlock(hashtextextended('$LOCK_LABEL', 0))" >/dev/null
echo "Gate 2 PASS: session 1 still holds (session-scoped isolation)"

# Gate 3: force-disconnect session 1 -> auto-release -> session 3 acquires
KC=$(docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "WITH killed AS (SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE query LIKE '%pg_sleep%' AND pid <> pg_backend_pid()) SELECT count(*) FROM killed") || KC=0
sleep 2
G3=$(docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_try_advisory_lock(hashtextextended('$LOCK_LABEL', 0))")
[ "$G3" = "t" ] || { echo "Gate 3 FAIL: lock should be released after disconnect (got $G3, killed=$KC)"; exit 1; }
docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_advisory_unlock(hashtextextended('$LOCK_LABEL', 0))" >/dev/null
echo "Gate 3 PASS: disconnect auto-released lock; session 3 acquired (t) [killed=$KC]"

# Gate 4: independent label does not collide
docker exec -d "$CONTAINER" psql -U postgres -d "$DBNAME" \
  -c "SELECT pg_try_advisory_lock(hashtextextended('$LOCK_LABEL', 0))" \
  -c "SELECT pg_sleep(8)" >/dev/null
sleep 2
G4=$(docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_try_advisory_lock(hashtextextended('mergepilot:other-label', 0))")
[ "$G4" = "t" ] || { echo "Gate 4 FAIL: independent label should acquire (got $G4)"; exit 1; }
docker exec "$CONTAINER" psql -U postgres -d "$DBNAME" -t -A -c \
  "SELECT pg_advisory_unlock(hashtextextended('mergepilot:other-label', 0))" >/dev/null
echo "Gate 4 PASS: independent label does not collide"

echo "=== M5-0A advisory lock integration: 4/4 PASS ==="
echo "hiclab_live=false (PG16 mechanics test)"
exit 0
