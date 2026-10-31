#!/usr/bin/env bash
# Isolation check (spec 007 FR-007, FR-008; spec 008 FR-002, SC-002; quickstart step 3).
#
# Needs the stack running:  docker compose up -d --build --wait
#
# Proves, from inside the running containers, that:
#   - no service can even resolve another service (they share no network), so the only
#     path between services is the Kafka broker, which every service can reach
#   - no service can reach another service's database (they are not on its network)
#   - Checkout and Inventory can reach their own
#   - only Catalog sees the catalog file, and not even Catalog can write it
#   - Analytics has no database at all
# Prints one line per check and exits non-zero on any violation.
set -uo pipefail
cd "$(dirname "$0")/.."

FAILED=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1" >&2; FAILED=1; }

reach() {  # <service> <host> -> exit 0 if the service can open a TCP connection to host:5432
  docker compose exec -T "$1" python -c \
    "import socket,sys; socket.create_connection(('$2', 5432), 2)" >/dev/null 2>&1
}

resolves() {  # <service> <host> -> exit 0 if the service can resolve the host name
  docker compose exec -T "$1" python -c \
    "import socket,sys; socket.getaddrinfo('$2', 8000)" >/dev/null 2>&1
}

echo "No service can reach another service (no shared network)"
for svc in catalog checkout inventory analytics; do
  for peer in catalog checkout inventory analytics; do
    [ "$svc" = "$peer" ] && continue
    if resolves "$svc" "$peer"; then
      bad "$svc can resolve $peer: services must meet only through Kafka"
    else
      ok "$svc cannot resolve $peer"
    fi
  done
done

echo "Every service can reach the broker"
for svc in catalog checkout inventory analytics; do
  if docker compose exec -T "$svc" python -c \
       "import socket; socket.create_connection(('kafka', 9092), 3)" >/dev/null 2>&1; then
    ok "$svc -> kafka:9092"
  else
    bad "$svc cannot reach kafka:9092"
  fi
done

echo "Databases reachable only by their owner"
for svc in catalog checkout inventory analytics; do
  for db in checkout-db inventory-db; do
    own=0
    [ "$svc-db" = "$db" ] && own=1
    if reach "$svc" "$db"; then
      [ $own -eq 1 ] && ok "$svc -> $db (its own)" || bad "$svc can reach $db, which is not its database"
    else
      [ $own -eq 1 ] && bad "$svc cannot reach its own database $db" || ok "$svc cannot reach $db"
    fi
  done
done

echo "The catalog file"
if docker compose exec -T catalog sh -c 'echo x >> /data/catalog.csv' >/dev/null 2>&1; then
  bad "catalog can write /data/catalog.csv (the volume must be read-only)"
else
  ok "catalog cannot write /data/catalog.csv"
fi
if docker compose exec -T catalog sh -c 'test -s /data/catalog.csv' >/dev/null 2>&1; then
  ok "catalog can read /data/catalog.csv"
else
  bad "catalog cannot read /data/catalog.csv"
fi
for svc in checkout inventory analytics; do
  if docker compose exec -T "$svc" sh -c 'test -e /data' >/dev/null 2>&1; then
    bad "$svc can see /data (only catalog may mount the catalog volume)"
  else
    ok "$svc has no /data"
  fi
done

echo "Analytics has no store"
if docker compose exec -T analytics env | grep -q 'DATABASE_URL'; then
  bad "analytics is given a DATABASE_URL"
else
  ok "analytics has no DATABASE_URL"
fi
if docker compose exec -T analytics python -c "import sqlalchemy" >/dev/null 2>&1; then
  bad "analytics image ships SQLAlchemy"
else
  ok "analytics image has no database library"
fi

[ $FAILED -eq 0 ] && echo "isolation: PASS" || { echo "isolation: FAIL" >&2; exit 1; }
