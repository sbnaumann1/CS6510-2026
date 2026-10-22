#!/usr/bin/env python
"""Service-boundary check (spec 007 FR-005, FR-007, FR-008, FR-022; SC-007).

    uv run python scripts/check_boundaries.py      # exit 1 on any violation

Rules (specs/007-microservices-architecture/contracts/service-boundaries.md):
   1. a service imports only its own package, the standard library, third-party libraries
      and checkout_web
   2. checkout_web stays data-free; Catalog and Analytics use no database library
   3. no `checkout_data` package exists, and nothing imports it
   4. Checkout's and Inventory's SQL touches only tables in their own models.py
   5. outbound HTTP goes through checkout_web.client, to the peers each service may call
   6. compose: each DATABASE_URL points at that service's own database, and Catalog and
      Analytics have none
   7. compose: a `*_data` network holds one service (plus its seed job); the catalog volume
      is mounted only into seed-catalog (read-write) and catalog (read-only)
   8. compose: no service's environment holds another database's credentials
   9. compose/nginx: the gateway never proxies /internal/
  10. each service's requirements.lock is its own
  11. compose: Analytics runs a single instance

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
RAW_HTTP_MODULES = ("requests", "aiohttp", "urllib.request", "http.client", "socket")
# Which peers each service may call (contracts/internal-apis.md).
ALLOWED_PEERS = {
    "catalog": set(),
    "checkout": {"catalog", "inventory"},
    "inventory": {"catalog"},
    "analytics": {"catalog", "checkout"},
}
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
                # Rule 5: raw HTTP, sockets.
                if any(_is(module, raw) for raw in RAW_HTTP_MODULES):
                    out.append(f"rule 5: {rel}:{line} imports {module}; use checkout_web.client")

            for node in ast.walk(tree):
                # Rule 5: constructing a raw httpx client in service code.
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "httpx"
                    and node.attr in {"AsyncClient", "Client", "get", "post", "put", "delete", "request"}
                ):
                    out.append(
                        f"rule 5: {rel}:{node.lineno} uses httpx.{node.attr}; use checkout_web.client"
                    )
                # Rule 5: a peer this service may not call.
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    for peer in _PEER_URL.findall(node.value):
                        if peer in SERVICES and peer != svc and peer not in ALLOWED_PEERS[svc]:
                            out.append(f"rule 5: {rel}:{node.lineno} calls {peer}, which {svc} may not")

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


def check(root: pathlib.Path) -> list[str]:
    """All violations under `root`, each prefixed `rule N:`."""
    return (
        _check_python(root)
        + _check_compose(root)
        + _check_gateway(root)
        + _check_locks(root)
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
