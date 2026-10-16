#!/bin/sh
# Remaining experiment matrix, run sequentially. Prototype is in the worktree source at start.
H=/Users/sbnaumann/.claude/jobs/fd6f61bd/tmp/harness
WT=/Users/sbnaumann/mscs-6510/CS6510-2026/.claude/worktrees/service-perf-investigation/backend
NAT="APPDIR=$WT APP=composite:app"
export APPDIR=$WT APP=composite:app APPDIR_FLAG="--app-dir $H/composite"
# native: 005 code (original) latency floor - restore originals first
cp "$H/orig/service.py" "$WT/services/checkout/src/checkout_service/service.py"
cp "$H/orig/checkout.py" "$WT/packages/checkout_data/src/checkout_data/repos/checkout.py"
STATIONS=1 DURATION=20 $H/native_exp.sh n1_native_1st
STATIONS=10 DURATION=30 $H/native_exp.sh n2_native_10st
# native with the prototype
cp "$H/proto/service.py" "$WT/services/checkout/src/checkout_service/service.py"
cp "$H/proto/checkout.py" "$WT/packages/checkout_data/src/checkout_data/repos/checkout.py"
$H/native_exp.sh n3_native_atomic100
STATIONS=1 DURATION=20 $H/native_exp.sh n4_native_atomic_1st
cp "$H/orig/service.py" "$WT/services/checkout/src/checkout_service/service.py"
cp "$H/orig/checkout.py" "$WT/packages/checkout_data/src/checkout_data/repos/checkout.py"
unset APPDIR APP APPDIR_FLAG
# docker
EXTRA="-f $H/w10.yaml" $H/run_exp.sh d3_w10
EXTRA="-f $H/httptools.yaml" GWLOG=1 $H/run_exp.sh d5_httptools
EXTRA="-f $H/proto.yaml" $H/run_exp.sh p1_atomic
CLIENT=net EXTRA="-f $H/proto.yaml -f $H/httptools.yaml" $H/run_exp.sh best_net_atomic_httptools
# graded config (assignment stress flags), baseline vs best-in-repo levers (atomic + httptools, host client)
STATIONS=100 DURATION=120 STOCK=10000 $H/run_exp.sh g_base_stress
STATIONS=100 DURATION=120 STOCK=10000 EXTRA="-f $H/proto.yaml -f $H/httptools.yaml" $H/run_exp.sh g_best_stress
docker compose -p perf -f $WT/compose.yaml down -v >/dev/null 2>&1
echo CHAIN DONE
