# Contract: Service HTTP Runtime and Gateway Header Handling

Amends [005 gateway-routes](../../005-service-architecture/contracts/gateway-routes.md) and
supersedes 005 research R10 ("h11 everywhere"). The route table, outage bodies and published
ports are unchanged.

## Services

- Every service image runs uvicorn with `--http ${UVICORN_HTTP}`, where
  `ENV UVICORN_HTTP=httptools` is set in the image. All other flags are unchanged:
  `--loop uvloop --workers ${WORKERS} --no-access-log --log-level warning`.
- `UVICORN_HTTP=h11` MUST be set for any deployment where a client sending `Upgrade: h2c`
  (e.g. `java.net.http.HttpClient` with default settings) can reach a service **without**
  the gateway (001 research R14).

## Gateway (`gateway/nginx.conf`, `http` block)

The shared proxy settings MUST include:

```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_set_header Upgrade "";          # new: never forward the client's h2c upgrade request
proxy_set_header HTTP2-Settings "";   # new
```

`tests/unit/test_gateway_routes.py` asserts all three `proxy_set_header` directives are
present, so the guarantee doesn't depend on nginx's default hop-by-hop handling.

## Direct service ports

`127.0.0.1:8081–8084` remain published, for `scripts/check_independence.sh` (curl) and for
debugging. They are **not** a supported entry point for the Java load client. The contract's
client uses the gateway URL only (005 FR-008).
