# Catalog Service image (specs/007-microservices-architecture/contracts/compose-topology.md).
# Build context: backend/. Copies only this service's directory plus checkout_web (the
# data-free shared code), so a change in another service never invalidates these layers.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SERVICE_NAME=catalog WORKERS=1 UVICORN_HTTP=httptools
WORKDIR /srv

# Third-party dependencies, pinned from the workspace uv.lock (cached layer).
COPY services/catalog/requirements.lock /tmp/req.lock
RUN pip install --no-cache-dir -r /tmp/req.lock

# checkout_web: error shape, request parsing, app factory, /healthz, money, request id and the
# service client. It knows no schema and imports no database library.
COPY packages/checkout_web /tmp/packages/checkout_web
RUN pip install --no-cache-dir --no-deps /tmp/packages/checkout_web

# This service only.
COPY services/catalog /tmp/svc
RUN pip install --no-cache-dir --no-deps /tmp/svc && rm -rf /tmp/packages /tmp/svc /tmp/req.lock

# The catalog file lives on a volume that compose mounts read-only here (and read-write
# only into the seed job): `python -m catalog_service.seed --if-empty` writes it.
ENV CATALOG_FILE=/data/catalog.csv

EXPOSE 8000
# Shell form on purpose, so ${WORKERS} and ${UVICORN_HTTP} expand.
# --http ${UVICORN_HTTP} (default httptools): spec 006 research R3; set
# UVICORN_HTTP=h11 if a client that sends Upgrade: h2c can reach this port directly.
CMD exec uvicorn catalog_service.main:app --host 0.0.0.0 --port 8000 \
    --workers ${WORKERS} --loop uvloop --http ${UVICORN_HTTP} --no-access-log --log-level warning
