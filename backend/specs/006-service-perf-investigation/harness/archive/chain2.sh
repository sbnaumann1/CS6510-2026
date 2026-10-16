#!/bin/sh
# Lock-order follow-ups (run after chain.sh).
H=/Users/sbnaumann/.claude/jobs/fd6f61bd/tmp/harness
WT=/Users/sbnaumann/mscs-6510/CS6510-2026/.claude/worktrees/service-perf-investigation/backend
EXTRA="-f $H/desc.yaml" $H/run_exp.sh p2_desc
EXTRA="-f $H/atomic_desc.yaml" $H/run_exp.sh p3_atomic_desc
STATIONS=100 DURATION=120 STOCK=10000 EXTRA="-f $H/atomic_desc.yaml -f $H/httptools.yaml" $H/run_exp.sh g_best2_stress
docker compose -p perf -f $WT/compose.yaml down -v >/dev/null 2>&1
echo CHAIN2 DONE
