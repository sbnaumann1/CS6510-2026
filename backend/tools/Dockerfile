# checkout-tools image: the cross-store verification tool (compose `tools`, on demand).
# Build context: backend/. It needs only httpx: the tool opens no database and asks each
# service for its own report over the mesh (spec 007 research R10).
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

RUN pip install --no-cache-dir httpx==0.25.2
COPY tools/verify_invariant.py /srv/verify_invariant.py

# Inside the stack the services are reached by name on the mesh network.
ENV CHECKOUT_URL=http://checkout:8000 INVENTORY_URL=http://inventory:8000 ANALYTICS_URL=http://analytics:8000

#   docker compose run --rm tools
CMD ["python", "-m", "verify_invariant"]
