#!/bin/sh
# Run N copies of cpubench in parallel; print each best time.  Usage: cpupar.sh PYTHON BENCH N
PY=$1; B=$2; N=$3
i=0; while [ $i -lt $N ]; do $PY $B & i=$((i+1)); done; wait
