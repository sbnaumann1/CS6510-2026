#!/bin/sh
# After chain2: native reruns (n1-n4 hit a stale server on :8090 the first time),
# same-session pre-005 native controls, then the layer microbenchmark.
H=/Users/sbnaumann/.claude/jobs/fd6f61bd/tmp/harness
WT=/Users/sbnaumann/mscs-6510/CS6510-2026/.claude/worktrees/service-perf-investigation/backend
SVC=$WT/services/checkout/src/checkout_service/service.py
REPO=$WT/packages/checkout_data/src/checkout_data/repos/checkout.py
docker compose -p perf -f $WT/compose.yaml down -v >/dev/null 2>&1
(
  export APPDIR=$WT APP=composite:app APPDIR_FLAG="--app-dir $H/composite"
  cp "$H/orig/service.py" "$SVC"; cp "$H/orig/checkout.py" "$REPO"
  STATIONS=1 DURATION=20 $H/native_exp.sh n1_native_1st
  STATIONS=10 DURATION=30 $H/native_exp.sh n2_native_10st
  $H/native_exp.sh n7_native100_rerun
  cp "$H/proto/service.py" "$SVC"; cp "$H/proto/checkout.py" "$REPO"
  $H/native_exp.sh n3_native_atomic100
  STATIONS=1 DURATION=20 $H/native_exp.sh n4_native_atomic_1st
  cp "$H/atomic_desc/checkout.py" "$REPO"
  $H/native_exp.sh n8_native_atomic_desc100
  cp "$H/orig/service.py" "$SVC"; cp "$H/desc/checkout.py" "$REPO"
  $H/native_exp.sh n9_native_desc100
  cp "$H/orig/service.py" "$SVC"; cp "$H/orig/checkout.py" "$REPO"
)
STATIONS=10 DURATION=30 $H/native_exp.sh n5_pre005_native_10st
STATIONS=1 DURATION=20 $H/native_exp.sh n6_pre005_native_1st
$H/native_exp.sh n10_pre005_native100_rerun
# the last native run leaves a seeded checkout_perf for the microbenchmark
cd $WT && DATABASE_URL="postgresql+asyncpg:///checkout_perf?host=/tmp" .venv/bin/python $H/layers_bench.py > /Users/sbnaumann/.claude/jobs/fd6f61bd/tmp/runs/layers_bench.txt 2>&1
echo CHAIN3 DONE
