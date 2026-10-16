#!/usr/bin/env bash
# Run a backend natively against host PG (Unix socket /tmp), in database checkout_perf.
# Usage: native_exp.sh NAME   env: STATIONS DURATION STOCK WORKERS DB_POOL_SIZE APPDIR APP APPDIR_FLAG
# Default: this checkout's 005+ services in one process (composite/composite.py).
# Pre-005 monolith: APPDIR=<its backend/> APP=app.main:app APPDIR_FLAG= (needs its own .venv).
set -uo pipefail
NAME=$1
H="$(cd "$(dirname "$0")" && pwd)"
BACK="${BACK:-$(cd "$H/../../.." && pwd)}"
LC="${LC:-$BACK/../load-client}"
RUNS="${PERF_RUNS:-$BACK/.perf-runs}"
APPDIR=${APPDIR:-$BACK}
APP=${APP:-composite:app}
APPDIR_FLAG=${APPDIR_FLAG---app-dir $H/composite}
OUT=$RUNS/$NAME
STATIONS=${STATIONS:-100}; DURATION=${DURATION:-30}; STOCK=${STOCK:-2000000}
export DATABASE_URL="postgresql+asyncpg:///checkout_perf?host=/tmp"
export STOCK_PER_ITEM=$STOCK CATALOG_SIZE=2000 WORKERS=${WORKERS:-4} DB_POOL_SIZE=${DB_POOL_SIZE:-12}
rm -rf "$OUT"; mkdir -p "$OUT"
docker compose -p perf -f "$BACK/compose.yaml" down -v >/dev/null 2>&1
cd "$APPDIR"
if lsof -ti :8090 >/dev/null 2>&1; then echo "$NAME: port 8090 busy - aborting" | tee "$OUT/summary.txt"; exit 1; fi
.venv/bin/python scripts/seed.py --reset > "$OUT/seed.log" 2>&1 || { echo seed failed; cat "$OUT/seed.log"; exit 1; }
.venv/bin/uvicorn $APP $APPDIR_FLAG --host 127.0.0.1 --port 8090 --workers $WORKERS --loop uvloop --http h11 --no-access-log --log-level warning > "$OUT/server.log" 2>&1 &
SRV=$!
for i in $(seq 1 60); do curl -sf http://127.0.0.1:8090/items >/dev/null && break; sleep 0.5; done
PG="postgresql:///checkout_perf?host=/tmp"
( printf "select extract(epoch from now()), coalesce(state,'-'), coalesce(wait_event_type,'-'), coalesce(wait_event,'-'), count(*) from pg_stat_activity where datname='checkout_perf' and backend_type='client backend' and pid<>pg_backend_pid() group by 2,3,4;\n\\\\watch 0.25\n" \
  | psql "$PG" -qAt -F, > "$OUT/pg_activity.csv" 2>/dev/null ) &
S1=$!
(cd "$LC" && java -cp out Main --baseUrl=http://127.0.0.1:8090 --stations=$STATIONS --duration=$DURATION --reportDir="$OUT") > "$OUT/client.log" 2>&1
{ kill $S1; pkill -f 'psql .*-qAt -F,'; wait $S1; } >/dev/null 2>&1
.venv/bin/python scripts/verify_invariant.py > "$OUT/invariant.txt" 2>&1; echo "invariant exit=$?" >> "$OUT/invariant.txt"
kill $SRV; sleep 3; pkill -9 -f "uvicorn .*--port 8090" 2>/dev/null; wait $SRV 2>/dev/null
# uvicorn's spawned workers survive a SIGKILL of the parent; kill whatever still holds the port
for p in $(lsof -ti :8090 2>/dev/null); do kill -9 $p 2>/dev/null; done
pkill -9 -f "$APPDIR/.venv/bin/python -c from multiprocessing" 2>/dev/null
sleep 1
python3 "$H/summ.py" "$OUT" | tee "$OUT/summary.txt"
