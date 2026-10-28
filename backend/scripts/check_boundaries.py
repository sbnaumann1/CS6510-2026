#!/usr/bin/env python
"""Service-boundary check (spec 007 FR-005, FR-007, FR-008, FR-022; spec 008 FR-002, FR-019).

    uv run python scripts/check_boundaries.py      # exit 1 on any violation

Rules (specs/007-microservices-architecture/contracts/service-boundaries.md, and
specs/008-event-driven-kafka/contracts/service-boundaries.md for E1-E10):
   1. a service imports only its own package, the standard library, third-party libraries
      and checkout_web
   2. checkout_web stays data-free; Catalog and Analytics use no database library
   3. no `checkout_data` package exists, and nothing imports it
   4. Checkout's and Inventory's SQL touches only tables in their own models.py
   6. compose: each DATABASE_URL points at that service's own database, and Catalog and
      Analytics have none
   7. compose: a `*_data` network holds one service (plus its seed job); the catalog volume
      is mounted only into seed-catalog (read-write) and catalog (read-only)
   8. compose: no service's environment holds another database's credentials
   9. compose/nginx: the gateway never proxies /internal/
  10. each service's requirements.lock is its own
  11. compose: Analytics runs a single instance
  (5 -- outbound HTTP to an allowed peer -- is gone: since spec 008 no service calls another)
  E1. no microservice code or configuration names another microservice's address
  E2. no HTTP client library (or socket) in a microservice, in code or dependencies
  E3. every topic a service uses is declared in events/topics.yaml and in its own events.py
  E4. a topic has exactly one producing service (dead letters excepted)
  E5. every consumed topic has a declared producer, and lists the consumer
  E6. only the owning service creates its event types
  E7. no two microservices share a Docker network
  E8. no /internal/ route other than /internal/reports/* in any service
  E9. every event type has a JSON Schema in events/schemas/
  E10. checkout_web imports no service package

Only `ast`, `re` and PyYAML (already installed with uvicorn[standard]): no new dependency,
and it runs as tests/unit/test_boundaries.py.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

SERVICES = ("catalog", "checkout", "inventory", "analytics")
DB_SERVICES = ("checkout", "inventory")  # the services that own a database
NO_DB_SERVICES = ("catalog", "analytics")

DB_LIBS = ("sqlalchemy", "asyncpg", "psycopg", "psycopg2")
# E2: nothing a microservice could call a peer with.
HTTP_MODULES = ("httpx", "requests", "aiohttp", "urllib.request", "urllib3", "http.client", "socket")
HTTP_DEPENDENCIES = ("httpx", "requests", "aiohttp", "urllib3")
SQL_MODULES = ("repo.py", "reserve.py", "seed.py")

_TABLE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|FROM|JOIN)\s+([a-z_][a-z0-9_]*)\b(?!\s*\()"
)
_TABLENAME = re.compile(r"__tablename__\s*=\s*[\"'](\w+)[\"']")
_CTE = re.compile(r"\b([a-z_][a-z0-9_]*)\s+AS\s+(?:MATERIALIZED\s+)?\(", re.I)
_PEER_URL = re.compile(r"https?://([a-z][a-z0-9_-]*)(?::\d+)?")


def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, node.lineno))
    return found


def _is(name: str, target: str) -> bool:
    return name == target or name.startswith(target + ".")


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _py_files(directory: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in directory.rglob("*.py") if "__pycache__" not in p.parts)


def _service_dir(root: pathlib.Path, svc: str) -> pathlib.Path:
    return root / "services" / svc / "src" / f"{svc}_service"


def _own_tables(root: pathlib.Path, svc: str) -> set[str]:
    models = _service_dir(root, svc) / "models.py"
    return set(_TABLENAME.findall(models.read_text())) if models.exists() else set()


def _check_python(root: pathlib.Path) -> list[str]:
    out: list[str] = []

    for svc in SERVICES:
        directory = _service_dir(root, svc)
        for path in _py_files(directory) if directory.exists() else []:
            rel = path.relative_to(root)
            tree = ast.parse(path.read_text(), str(path))
            docstrings = _docstrings(tree)

            for module, line in _imports(tree):
                top = module.split(".")[0]
                # Rule 1: another service's package, or the removed shared data library.
                if top.endswith("_service") and top != f"{svc}_service":
                    out.append(f"rule 1: {rel}:{line} imports {module} (another service)")
                # Rule 2: no database library in a service that has no database.
                if svc in NO_DB_SERVICES and any(_is(module, lib) for lib in DB_LIBS):
                    out.append(f"rule 2: {rel}:{line} imports {module} but {svc} has no database")
                # E2: no HTTP client library or socket in a microservice.
                if any(_is(module, http) for http in HTTP_MODULES):
                    out.append(f"E2: {rel}:{line} imports {module}; services talk through events only")

            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    # E1: another microservice's address.
                    for peer in _PEER_URL.findall(node.value):
                        if peer in SERVICES and peer != svc:
                            out.append(f"E1: {rel}:{node.lineno} names {peer}'s address ({node.value!r})")
                    # E8: only /internal/reports/* may exist beside the public routes.
                    if node.value.startswith("/internal/") and not node.value.startswith("/internal/reports/"):
                        out.append(f"E8: {rel}:{node.lineno} defines {node.value!r}; only /internal/reports/* is allowed")

            # Rule 4: SQL only touches tables in this service's own models.py.
            if svc in DB_SERVICES and path.name in SQL_MODULES:
                own = _own_tables(root, svc)
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                        continue
                    if id(node) in docstrings:
                        continue
                    sql = node.value
                    ctes = {m.lower() for m in _CTE.findall(sql)}
                    for table in _TABLE.findall(sql):
                        if table in ctes or table in own:
                            continue
                        out.append(
                            f"rule 4: {rel}:{node.lineno} names table {table!r}, "
                            f"which is not in {svc}'s models.py"
                        )

    # Rule 2: checkout_web is data-free.
    web = root / "packages" / "checkout_web" / "src" / "checkout_web"
    for path in _py_files(web) if web.exists() else []:
        tree = ast.parse(path.read_text(), str(path))
        for module, line in _imports(tree):
            if any(_is(module, lib) for lib in DB_LIBS):
                out.append(f"rule 2: {path.relative_to(root)}:{line} imports {module} (checkout_web is data-free)")
            if module.split(".")[0].endswith("_service"):
                out.append(f"E10: {path.relative_to(root)}:{line} imports {module}; checkout_web knows no service")

    # Rule 3: the shared data library must be gone, and not imported anywhere.
    if (root / "packages" / "checkout_data").exists():
        out.append("rule 3: packages/checkout_data still exists")
    for base in ("services", "packages", "scripts", "tools"):
        directory = root / base
        for path in _py_files(directory) if directory.exists() else []:
            tree = ast.parse(path.read_text(), str(path))
            for module, line in _imports(tree):
                if _is(module, "checkout_data"):
                    out.append(f"rule 3: {path.relative_to(root)}:{line} imports {module}")
    return out


def _load_compose(root: pathlib.Path) -> dict | None:
    path = root / "compose.yaml"
    if not path.exists():
        return None
    import yaml

    return yaml.safe_load(path.read_text())


def _env(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {k: str(v) for k, v in env.items()}


def _url_host(url: str) -> str:
    m = re.search(r"@([^:/@]+)(?::\d+)?/", url)
    return m.group(1) if m else ""


def _check_compose(root: pathlib.Path) -> list[str]:
    compose = _load_compose(root)
    if compose is None:
        return []
    out: list[str] = []
    services: dict[str, dict] = compose.get("services", {})
    own_db = {"checkout": "checkout-db", "inventory": "inventory-db"}

    # Rule 6: each DATABASE_URL points at the service's own database; none for the others.
    hosts_used: dict[str, str] = {}
    for name, svc in services.items():
        owner = name.removeprefix("seed-")
        if owner not in SERVICES:
            continue
        urls = {k: v for k, v in _env(svc).items() if k.endswith("DATABASE_URL")}
        if owner in NO_DB_SERVICES and urls:
            out.append(f"rule 6: {name} is given a database ({', '.join(urls)}) but {owner} has none")
        for key, url in urls.items():
            host = _url_host(url)
            if owner in own_db and host != own_db[owner]:
                out.append(f"rule 6: {name} {key} points at {host!r}, not {own_db[owner]!r}")
            if host in hosts_used and hosts_used[host] != owner:
                out.append(f"rule 6: {host} is used by both {hosts_used[host]} and {owner}")
            hosts_used.setdefault(host, owner)

    # E1: no service is told another service's address.
    for name, svc in services.items():
        if name not in SERVICES:
            continue
        for key, value in _env(svc).items():
            if key.endswith("DATABASE_URL"):
                continue
            for peer in _PEER_URL.findall(value):
                if peer in SERVICES and peer != name:
                    out.append(f"E1: compose {name} {key} names {peer}'s address ({value!r})")
            if key.endswith("_URL") and key.removesuffix("_URL").lower() in SERVICES:
                out.append(f"E1: compose {name} is given {key}, the address of another service")

    # E7: no two microservices share a network.
    on_net: dict[str, set[str]] = {}
    for name, svc in services.items():
        nets = svc.get("networks") or []
        for net in nets if isinstance(nets, list) else list(nets):
            if name in SERVICES:
                on_net.setdefault(net, set()).add(name)
    for net, names in sorted(on_net.items()):
        if len(names) > 1:
            out.append(f"E7: {', '.join(sorted(names))} share the network {net!r}")

    # Rule 7: a database network belongs to one service (plus its database and seed job).
    members: dict[str, set[str]] = {}
    for name, svc in services.items():
        nets = svc.get("networks") or []
        for net in nets if isinstance(nets, list) else list(nets):
            members.setdefault(net, set()).add(name)
    for net, names in members.items():
        if not net.endswith("_data"):
            continue
        owner = net.removesuffix("_data")
        allowed = {owner, f"{owner}-db", f"seed-{owner}"}
        for name in sorted(names - allowed):
            out.append(f"rule 7: {name} is attached to {net}, which belongs to {owner}")
    for name, svc in services.items():
        for volume in svc.get("volumes") or []:
            text = volume if isinstance(volume, str) else f"{volume.get('source')}:{volume.get('target')}"
            if not text.startswith("catalog_data:"):
                continue
            read_only = text.endswith(":ro") or (not isinstance(volume, str) and volume.get("read_only"))
            if name == "catalog" and read_only:
                continue
            if name == "seed-catalog" and not read_only:
                continue
            out.append(f"rule 7: {name} mounts catalog_data{' read-only' if read_only else ' read-write'}")

    # Rule 8: no unit holds another database's credentials.
    secrets = {
        "checkout": ("CHECKOUT_DB_", "checkout_user", "checkout_pass"),
        "inventory": ("INVENTORY_DB_", "inventory_user", "inventory_pass"),
    }
    for name, svc in services.items():
        owner = name.removeprefix("seed-").removesuffix("-db")
        blob = " ".join(f"{k}={v}" for k, v in _env(svc).items())
        for db, needles in secrets.items():
            if owner == db:
                continue
            if any(n in blob for n in needles):
                out.append(f"rule 8: {name} holds {db}'s database credentials")

    # Rule 11: Analytics is a single instance.
    analytics = services.get("analytics")
    if analytics is not None:
        if _env(analytics).get("WORKERS", "1") != "1":
            out.append("rule 11: analytics WORKERS is not 1")
        replicas = (analytics.get("deploy") or {}).get("replicas", 1)
        if int(replicas) > 1:
            out.append(f"rule 11: analytics deploy.replicas is {replicas}")
    return out


def _check_gateway(root: pathlib.Path) -> list[str]:
    conf = root / "gateway" / "nginx.conf"
    if not conf.exists():
        return []
    text = conf.read_text()
    block = re.search(r"location\s+/internal/\s*\{(.*?)\}", text, re.S)
    if block is None:
        return ["rule 9: gateway/nginx.conf has no /internal/ location; the default route would proxy it"]
    if "proxy_pass" in block.group(1):
        return ["rule 9: gateway/nginx.conf proxies /internal/"]
    return []


def _check_locks(root: pathlib.Path) -> list[str]:
    out = []
    for svc in SERVICES:
        lock = root / "services" / svc / "requirements.lock"
        if not lock.exists():
            continue
        names = {
            line.split("==")[0].split("[")[0].strip().lower()
            for line in lock.read_text().splitlines()
            if line and not line.startswith(("#", " ", "-"))
        }
        foreign = {f"{o}-service" for o in SERVICES if o != svc} | {"checkout-data"}
        for name in sorted(names & foreign):
            out.append(f"rule 10: services/{svc}/requirements.lock contains {name}")
        if svc in NO_DB_SERVICES and names & set(DB_LIBS) | names & {"psycopg2-binary"}:
            out.append(f"rule 10: services/{svc}/requirements.lock ships a database driver")
    return out


def _check_dependencies(root: pathlib.Path) -> list[str]:
    """E2 in the manifests: no HTTP client library is a dependency of a microservice."""
    out = []
    for svc in SERVICES:
        manifest = root / "services" / svc / "pyproject.toml"
        if manifest.exists():
            names = {
                re.split(r"[=<>!~\[\s]", line.strip().strip('",'))[0].lower()
                for line in manifest.read_text().splitlines()
                if line.strip().startswith('"')
            }
            for dep in sorted(names & set(HTTP_DEPENDENCIES)):
                out.append(f"E2: services/{svc}/pyproject.toml depends on {dep}")
        lock = root / "services" / svc / "requirements.lock"
        if lock.exists():
            locked = {
                line.split("==")[0].split("[")[0].strip().lower()
                for line in lock.read_text().splitlines()
                if line and not line.startswith(("#", " ", "-"))
            }
            for dep in sorted(locked & set(HTTP_DEPENDENCIES)):
                out.append(f"E2: services/{svc}/requirements.lock ships {dep}")
    return out


_CONST = re.compile(r'^([A-Z][A-Z_]*)\s*=\s*"([^"]+)"\s*$', re.M)


def _tuple_names(tree: ast.AST, target: str) -> list[str] | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) or isinstance(node, ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == target for t in targets) and isinstance(node.value, ast.Tuple):
                return [e.id for e in node.value.elts if isinstance(e, ast.Name)]
    return None


def _check_events(root: pathlib.Path) -> list[str]:
    """E3-E6 and E9: the event topology in code matches events/topics.yaml and the schemas."""
    manifest_path = root / "events" / "topics.yaml"
    events_py = root / "packages" / "checkout_web" / "src" / "checkout_web" / "events.py"
    if not manifest_path.exists() or not events_py.exists():
        return []
    import yaml

    out: list[str] = []
    topics: dict[str, dict] = yaml.safe_load(manifest_path.read_text())["topics"]
    consts = dict(_CONST.findall(events_py.read_text()))
    topic_const = {name: value for name, value in consts.items() if value in topics}
    type_const = {
        name: value
        for name, value in consts.items()
        if re.fullmatch(r"(?:catalog|checkout|inventory|analytics)\.[a-z-]+|dead-letter", value)
    }

    # E9: every event type has a schema, and the envelope has one.
    schemas = root / "events" / "schemas"
    for value in sorted(set(type_const.values())):
        if not (schemas / f"{value}.json").exists():
            out.append(f"E9: events/schemas/{value}.json is missing")
    if not (schemas / "envelope.json").exists():
        out.append("E9: events/schemas/envelope.json is missing")

    produces: dict[str, set[str]] = {}
    consumes: dict[str, set[str]] = {}
    for svc in SERVICES:
        directory = _service_dir(root, svc)
        decl = directory / "events.py"
        if not decl.exists():
            continue
        tree = ast.parse(decl.read_text(), str(decl))
        prod = _tuple_names(tree, "PRODUCES") or []
        cons = _tuple_names(tree, "CONSUMES") or []
        produces[svc] = {topic_const[n] for n in prod if n in topic_const}
        consumes[svc] = {topic_const[n] for n in cons if n in topic_const}
        for n in prod + cons:
            if n not in topic_const:
                out.append(f"E3: {decl.relative_to(root)} lists {n}, which is not a topic in events/topics.yaml")

        declared = produces[svc] | consumes[svc] | {"dead-letters.v1"}
        for path in _py_files(directory):
            if path.name == "events.py":
                continue
            tree = ast.parse(path.read_text(), str(path))
            docstrings = _docstrings(tree)
            for node in ast.walk(tree):
                name = node.id if isinstance(node, ast.Name) else None
                if name in topic_const and topic_const[name] not in declared:
                    out.append(
                        f"E3: {path.relative_to(root)}:{node.lineno} uses topic {topic_const[name]}, "
                        f"which {svc}'s events.py does not declare"
                    )
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in topics
                    and id(node) not in docstrings
                ):
                    out.append(
                        f"E3: {path.relative_to(root)}:{node.lineno} writes the topic name {node.value!r} "
                        "as a literal; use the constant from checkout_web.events"
                    )
                # E6: only the owner creates its event types.
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "make_event"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in type_const
                ):
                    etype = type_const[node.args[0].id]
                    if etype != "dead-letter" and not etype.startswith(f"{svc}."):
                        out.append(
                            f"E6: {path.relative_to(root)}:{node.lineno} creates {etype}, "
                            f"which belongs to {etype.split('.')[0]}"
                        )

    # E4 / E5: producers and consumers, in the manifest and in the services' own declarations.
    for topic, spec in sorted(topics.items()):
        producer = spec.get("producer")
        if not producer:
            out.append(f"E5: {topic} has no declared producer")
            continue
        claimed = sorted(s_ for s_, tps in produces.items() if topic in tps)
        if producer == "*":
            continue
        if claimed != [producer]:
            out.append(f"E4: {topic} is owned by {producer} but produced by {claimed or 'nobody'}")
    for svc, tps in sorted(consumes.items()):
        for topic in sorted(tps):
            spec = topics.get(topic)
            if spec is None or not spec.get("producer"):
                out.append(f"E5: {svc} consumes {topic}, which has no declared producer")
            elif svc not in (spec.get("consumers") or []):
                out.append(f"E5: {svc} consumes {topic} but events/topics.yaml does not list it as a consumer")
    for topic, spec in sorted(topics.items()):
        for consumer in spec.get("consumers") or []:
            if consumer in consumes and topic not in consumes[consumer]:
                out.append(f"E5: events/topics.yaml lists {consumer} as a consumer of {topic}, but its events.py does not")
    return out


def check(root: pathlib.Path) -> list[str]:
    """All violations under `root`, each prefixed `rule N:` or `E<n>:`."""
    return (
        _check_python(root)
        + _check_compose(root)
        + _check_gateway(root)
        + _check_locks(root)
        + _check_dependencies(root)
        + _check_events(root)
    )


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    violations = check(root)
    for v in violations:
        print(v)
    print(f"{len(violations)} boundary violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
