# Contract: Service Boundaries (automated check)

`scripts/check_boundaries.py` fails the build on any violation. It keeps 007's eleven rules
(no cross-service imports, no foreign database connections, no shared stores or credentials, no
shared data-access code, and so on) and adds these:

| # | Rule | How it is checked |
| --- | --- | --- |
| E1 | No microservice code or configuration names another microservice's address | scan `services/*` and `compose.yaml` for `http://catalog`, `http://checkout`, `http://inventory`, `http://analytics` and `*_URL` variables other than the broker and the service's own database |
| E2 | No HTTP client library in a microservice | no `httpx`, `requests` or `aiohttp` import or dependency in `services/*` (the tools image may use `httpx`) |
| E3 | Every topic a service references is declared in `events/topics.yaml` and in the service's `events.py` | compare constants, `topics.yaml` and `events.py` |
| E4 | A topic has exactly one producer service (dead letters excepted) | `topics.yaml` and `events.py` PRODUCES lists |
| E5 | Every consumed topic has a declared producer | `topics.yaml` and `events.py` CONSUMES lists |
| E6 | Only the owning service publishes its event types | event `type` prefix equals the producing service |
| E7 | No two services share a Docker network | parse `compose.yaml` networks |
| E8 | No `/internal/` route other than `/internal/reports/*` exists in any service | scan routers |
| E9 | Every event type has a schema in `events/schemas/` | list `events.py` types against files |
| E10 | The shared package holds no data access and no business events | `checkout_web` imports no database library and defines only the envelope and the bus |

Shared code allowed in `checkout_web` (listed explicitly, per 007 FR-005): errors, request
parsing, app factory, `/healthz`, money helpers, **event envelope and topic-name constants
(`events`), `Bus` with its Kafka and in-memory implementations (`bus`), `consumer_loop`, the
generic catalog-topic reader (`snapshot`) and task shutdown (`lifecycle`)**. None of it holds a
schema, a table or a query; each service owns its own outbox tables and relay.

Rule 5 of spec 007 (outbound HTTP only through `checkout_web.client`, only to allowed peers) no
longer exists: the client is deleted and E1/E2 forbid any service-to-service call. The tools
image (operator checks) keeps `httpx` and is not a microservice.
