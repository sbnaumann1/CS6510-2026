#!/usr/bin/env bash
# Sample per-container CPU and memory once per cycle until the file STOP appears or
# SECONDS_MAX elapses (spec 009 profiling). CSV: epoch_s,container,cpu_percent,mem_percent
# CPU is "percent of one core": 250 means 2.5 cores busy.
#   scripts/sample_stats.sh out.csv /tmp/stop-file [max_seconds]
set -u
OUT="$1"; STOP="$2"; MAX="${3:-600}"
: > "$OUT"
end=$((SECONDS + MAX))
while [ ! -e "$STOP" ] && [ $SECONDS -lt $end ]; do
  now=$(date +%s)
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemPerc}}' 2>/dev/null \
    | sed 's/%//g' | awk -F, -v t="$now" 'NF==3 {print t","$1","$2","$3}' >> "$OUT"
done
