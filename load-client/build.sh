#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
mkdir -p out
javac -d out src/*.java
echo "Built. Run with: ./run.sh [--key=value ...]   (or: java -cp out Main --help)"
