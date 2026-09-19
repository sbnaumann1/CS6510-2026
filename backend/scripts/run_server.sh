#!/usr/bin/env bash
# Launch the API for load testing. Keep-alive stays enabled: the Java load client
# reuses connections (contracts/README.md).
#
# --http h11 is REQUIRED, not a preference. java.net.http.HttpClient defaults to
# HTTP_2, so every POST carries `Connection: Upgrade, HTTP2-Settings` + `Upgrade:
# h2c`. uvicorn's httptools parser treats that as an upgrade request, logs
# "Unsupported upgrade request", and never delivers the request body to the app —
# every POST /transactions then fails 400 INVALID_REQUEST. GET is unaffected
# (no body to lose). h11 parses the same request correctly. See research R14.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --workers "${WORKERS:-4}" \
  --loop uvloop \
  --http h11 \
  --no-access-log \
  --log-level warning
