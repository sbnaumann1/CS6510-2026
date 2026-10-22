# Contract: Service Boundaries

Enforced by `scripts/check_boundaries.py` as part of the test suite. Replaces 005's table-ownership
rules, which no longer apply: there is no shared database to own tables in.

| # | Rule | Fails when |
| --- | --- | --- |
| 1 | A service imports only its own package, the standard library, third-party libraries, and `checkout_web` | it imports another service's package |
| 2 | `checkout_web` stays data-free | it imports `sqlalchemy`, `asyncpg` or `psycopg` |
| 3 | No `checkout_data` package exists | the directory or an import of it is present |
| 4 | Each service's SQL lives in its own `repo.py` / `reserve.py` / `seed.py` and touches only tables in its own `models.py` | a statement names a table that is not in that service's models |
| 5 | Outbound HTTP only through `checkout_web.client` to the callees allowed in [internal-apis.md](./internal-apis.md) | a service calls an unlisted peer, or uses a raw HTTP client |
| 6 | Compose: each `DATABASE_URL` host is that service's own `*-db` | a URL points at another service's database, or two services share one |
| 7 | Compose: a `*_data` network has exactly one service member (plus its seed job) | any other service is attached |
| 8 | Compose: no service's environment holds another database's credentials | a foreign `*_user` / `*_pass` appears |
| 9 | Compose: gateway routes contain no `/internal/` upstream | `/internal/` is proxied |
| 10 | Each service's `requirements.lock` is its own | one lock is shared, or another service's package appears in it |

SQL attribution (rule 4) is checked by scanning `text(...)` literals for table names and comparing
with `Base.metadata.tables` of that service.

**Accepted shared code** (FR-005), exhaustive: `checkout_web` provides the contract error shape,
request parsing, the app factory, `/healthz`, money formatting and the HTTP client. None of it
knows a schema.
