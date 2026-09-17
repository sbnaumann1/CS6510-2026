#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d out ]; then
  echo "Not built yet - running build.sh first..."
  ./build.sh
fi
java -cp out Main "$@"
