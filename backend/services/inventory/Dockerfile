# Inventory Service image (specs/005-service-architecture/research.md R3).
# Build context: backend/. Copies only this service's directory plus the shared
# libraries, so a change in another service never invalidates these layers.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SERVICE_NAME=inventory WORKERS=1 UVICORN_HTTP=httptools
WORKDIR /srv

# Third-party dependencies, pinned from the workspace uv.lock (cached layer).
COPY services/inventory/requirements.lock /tmp/req.lock
RUN pip install --no-cache-dir -r /tmp/req.lock

# The shared libraries: the deliberate coupling point, distributed into every image.
COPY packages/checkout_data /tmp/packages/checkout_data
COPY packages/checkout_web /tmp/packages/checkout_web
RUN pip install --no-cache-dir --no-deps /tmp/packages/checkout_data /tmp/packages/checkout_web

# This service only.
COPY services/inventory /tmp/svc
RUN pip install --no-cache-dir --no-deps /tmp/svc && rm -rf /tmp/packages /tmp/svc /tmp/req.lock

EXPOSE 8000
# Shell form on purpose, so ${WORKERS} and ${UVICORN_HTTP} expand.
# --http ${UVICORN_HTTP} (default httptools): spec 006 research R3; set
# UVICORN_HTTP=h11 if a client that sends Upgrade: h2c can reach this port directly.
CMD exec uvicorn inventory_service.main:app --host 0.0.0.0 --port 8000 \
    --workers ${WORKERS} --loop uvloop --http ${UVICORN_HTTP} --no-access-log --log-level warning
