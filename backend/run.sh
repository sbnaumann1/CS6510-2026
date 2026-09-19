#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8080}"
if [ ! -d .venv ]; then
  echo "Not installed yet - running uv sync first..."
  uv sync
fi
# Check the port BEFORE seeding: seeding drops the schema, so doing it while
# another instance is live corrupts that instance and then fails to bind anyway.
if lsof -ti :"$PORT" >/dev/null 2>&1; then
  echo "Something is already listening on port $PORT."
  if [ -t 0 ]; then
    read -r -p "Stop it and restart with a fresh database? [y/N] " reply
  else
    reply=n
  fi
  case "$reply" in
    [yY]*)
      lsof -ti :"$PORT" | xargs kill 2>/dev/null || true
      sleep 2
      lsof -ti :"$PORT" | xargs kill -9 2>/dev/null || true
      sleep 1 ;;
    *)
      echo "Left it running. Stop it yourself, or set PORT=<other> to run alongside." >&2
      exit 1 ;;
  esac
fi
# Like MockServer's constructor, every start rebuilds the catalog and stock.
# Use scripts/run_server.sh to restart without touching existing data.
uv run python scripts/seed.py --reset
# --http h11 is required, not a preference: the Java load client sends
# `Upgrade: h2c`, which makes uvicorn's httptools parser drop the request body
# and fail every POST. See specs/001-checkout-backend/research.md R14.
exec uv run uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
  --workers "${WORKERS:-4}" --loop uvloop --http h11 --no-access-log --log-level warning
