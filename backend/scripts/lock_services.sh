#!/usr/bin/env bash
# Export one pinned requirements file per deployable image from the workspace uv.lock
# (005 research R3). Each Dockerfile installs only its own file, so a change in one
# service never invalidates another service's image layers.
#
#   ./scripts/lock_services.sh          # regenerate after any `uv lock`
#
# tests/unit/test_service_locks.py fails if a committed file is stale.
set -euo pipefail
cd "$(dirname "$0")/.."

export_lock() {  # <package> <output>
  uv export --frozen --no-dev --no-emit-workspace --no-hashes --no-header \
    --package "$1" -o "$2" >/dev/null
  echo "wrote $2"
}

for svc in catalog checkout inventory analytics; do
  export_lock "${svc}-service" "services/${svc}/requirements.lock"
done
