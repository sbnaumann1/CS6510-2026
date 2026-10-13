# checkout-tools image: the seed and invariant-check CLIs (compose `seed` and `tools`).
# Build context: backend/.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv

COPY packages/checkout_data/requirements.lock /tmp/req.lock
RUN pip install --no-cache-dir -r /tmp/req.lock

COPY packages/checkout_data /tmp/packages/checkout_data
RUN pip install --no-cache-dir --no-deps /tmp/packages/checkout_data && rm -rf /tmp/packages /tmp/req.lock

# Seeds only an empty database (research R7); a full reset is explicit:
#   docker compose run --rm tools python -m checkout_data.seed --reset
CMD ["python", "-m", "checkout_data.seed", "--if-empty"]
