#!/usr/bin/env bash
# Usage: run_exp.sh NAME   env: STATIONS DURATION STOCK EXTRA (extra -f files) CLIENT=host|net GWLOG=1
# Paths (all optional): BACK (backend/), LC (load-client/), PERF_RUNS (output root). See README.md.
set -uo pipefail
NAME=$1
H="$(cd "$(dirname "$0")" && pwd)"
BACK="${BACK:-$(cd "$H/../../.." && pwd)}"
LC="${LC:-$BACK/../load-client}"
RUNS="${PERF_RUNS:-$BACK/.perf-runs}"
export PERF_RUNS="$RUNS"   # interpolated by gwlog.yaml
OUT=$RUNS/$NAME
STATIONS=${STATIONS:-100}; DURATION=${DURATION:-30}; STOCK=${STOCK:-2000000}
CLIENT=${CLIENT:-host}; EXTRA=${EXTRA:-}; GWLOG=${GWLOG:-0}
PG="postgresql://checkout:checkout@127.0.0.1:5433/checkout"
rm -rf "$OUT"; mkdir -p "$OUT"
cd "$BACK"
FILES="-f compose.yaml -f $H/instrument.yaml"
[ "$GWLOG" = 1 ] && { rm -rf "$RUNS/_nginxlog"; mkdir -p "$RUNS/_nginxlog"; FILES="$FILES -f $H/gwlog.yaml"; }
FILES="$FILES $EXTRA"
DC="docker compose -p perf $FILES"
echo "$DC" > "$OUT/compose-cmd.txt"
env | grep -E '^(STATIONS|DURATION|STOCK|CLIENT|EXTRA|GWLOG)=' > "$OUT/env.txt"

$DC down -v >/dev/null 2>&1
STOCK_PER_ITEM=$STOCK $DC up -d --build --wait >"$OUT/up.log" 2>&1 || { echo "up failed"; tail -30 "$OUT/up.log"; exit 1; }
psql "$PG" -qAt -c "create extension if not exists pg_stat_statements" >/dev/null
sleep 2
psql "$PG" -qAt -c "select pg_stat_statements_reset(); select pg_stat_reset();" >/dev/null

# samplers
( printf "select extract(epoch from now()), coalesce(state,'-'), coalesce(wait_event_type,'-'), coalesce(wait_event,'-'), count(*) from pg_stat_activity where backend_type='client backend' and pid<>pg_backend_pid() group by 2,3,4;\n\\\\watch 0.25\n" \
  | psql "$PG" -qAt -F, > "$OUT/pg_activity.csv" 2>/dev/null ) &
S1=$!
( while true; do docker stats --no-stream --format '{{.Name}},{{.CPUPerc}}' 2>/dev/null | sed "s/^/$(date +%s),/"; done > "$OUT/docker_cpu.csv" ) &
S2=$!
( while true; do ps -A -o %cpu=,comm= | awk -v t=$(date +%s) '{c=$1; $1=""; print t","c","$0}' | grep -E 'java|Virtualization|vmnetd|com.docker' ; sleep 2; done > "$OUT/host_cpu.csv" ) &
S3=$!

if [ "$CLIENT" = net ]; then
  docker run --rm --network perf_default -v "$LC":/lc -v "$OUT":/out -w /lc eclipse-temurin:21-jre \
    java -cp out Main --baseUrl=${NET_URL:-http://gateway:8080} --stations=$STATIONS --duration=$DURATION --reportDir=/out > "$OUT/client.log" 2>&1
else
  (cd "$LC" && java -cp out Main --baseUrl=${BASE_URL:-http://localhost:8080} --stations=$STATIONS --duration=$DURATION --reportDir="$OUT") > "$OUT/client.log" 2>&1
fi

{ kill $S1 $S2 $S3; pkill -P $S2; pkill -P $S3; pkill -f 'psql .*-qAt -F,'; wait $S1 $S2 $S3; } >/dev/null 2>&1
psql "$PG" -A -F$'\t' -c "select calls, round(total_exec_time::numeric,1) total_ms, round(mean_exec_time::numeric,3) mean_ms, round(stddev_exec_time::numeric,3) sd_ms, rows, shared_blks_hit, shared_blks_read, left(regexp_replace(query,'\s+',' ','g'),160) q from pg_stat_statements order by total_exec_time desc limit 30" > "$OUT/pg_statements.tsv"
psql "$PG" -A -F$'\t' -c "select xact_commit, xact_rollback, deadlocks, blks_hit, blks_read, tup_updated, tup_inserted from pg_stat_database where datname='checkout'" > "$OUT/pg_db.tsv"
psql "$PG" -A -F$'\t' -c "select relname, n_tup_upd, n_tup_hot_upd, n_dead_tup, n_live_tup, autovacuum_count from pg_stat_user_tables order by n_tup_upd desc" > "$OUT/pg_tables.tsv"
if [ "$GWLOG" = 1 ]; then sleep 2; $DC exec -T gateway nginx -s reopen >/dev/null 2>&1; sleep 1; cp "$RUNS/_nginxlog/timing.log" "$OUT/nginx_timing.log" 2>/dev/null; fi
$DC run --rm tools python -m checkout_data.verify_invariant > "$OUT/invariant.txt" 2>&1; echo "invariant exit=$?" >> "$OUT/invariant.txt"
$DC logs --no-color > "$OUT/compose.log" 2>&1
python3 $H/summ.py "$OUT" | tee "$OUT/summary.txt"
[ -f "$OUT/nginx_timing.log" ] && python3 $H/nginx_summ.py "$OUT/nginx_timing.log" | tee -a "$OUT/summary.txt"
cut -f1-5,8 "$OUT/pg_statements.tsv" | head -9 | cut -c1-170 | tee -a "$OUT/summary.txt"
tail -n +2 "$OUT/pg_db.tsv" | head -1 | tee -a "$OUT/summary.txt"
