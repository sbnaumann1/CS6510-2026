#!/usr/bin/env python
"""Service-boundary check (spec FR-009, FR-011, FR-016; research R12).

    uv run python scripts/check_boundaries.py      # exit 1 on any violation

Rules (specs/005-service-architecture/contracts/service-boundaries.md):
  1. no service imports another service's package
  2. no service imports an HTTP client or socket module (no service-to-service calls)
  3. a service imports only its own checkout_data.repos module, plus repos.catalog
  4. no SQL construction in services (it lives only in checkout_data)
  5. each checkout_data/repos/<svc>.py writes only tables <svc> owns and reads
     only tables it is a declared reader of (checkout_data.ownership.TABLES)
  6. no domain service's compose environment names another service's host
  7. checkout_data never imports fastapi/checkout_web or raises ApiError

Only `ast` and `re`: no new dependency, and it runs as tests/unit/test_boundaries.py.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

from checkout_data.ownership import SERVICES, TABLES

HTTP_MODULES = ("httpx", "requests", "aiohttp", "urllib.request", "http.client", "socket")
SQL_NAMES = {"text", "select", "insert", "update", "delete"}
SHARED_REPO = "catalog"
HOSTS = (*SERVICES, "gateway")

_WRITE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.I)
_READ = re.compile(r"\b(?:FROM|JOIN)\s+(\w+)", re.I)


def _imports(tree: ast.AST) -> list[tuple[str, list[str], int]]:
    """(module, imported names, line) for every import statement."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, [], node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.module, [a.name for a in node.names], node.lineno))
    return found


def _is_module(name: str, target: str) -> bool:
    return name == target or name.startswith(target + ".")


def _check_service(svc: str, path: pathlib.Path, rel: str) -> list[str]:
    out = []
    tree = ast.parse(path.read_text(), str(path))
    for module, names, line in _imports(tree):
        top = module.split(".")[0]
        # Rule 1: another service's package.
        if top.endswith("_service") and top != f"{svc}_service":
            out.append(f"rule 1: {rel}:{line} imports {module} (another service)")
        # Rule 2: outbound network clients.
        candidates = [module] + [f"{module}.{n}" for n in names]
        for target in HTTP_MODULES:
            if any(_is_module(c, target) for c in candidates):
                out.append(f"rule 2: {rel}:{line} imports {target} (network client)")
                break
        # Rule 3: only its own repos module and the shared catalog reader.
        repos = []
        if module == "checkout_data.repos":
            repos = names
        elif module.startswith("checkout_data.repos."):
            repos = [module.split(".")[2]]
        for repo in repos:
            if repo not in (svc, SHARED_REPO):
                out.append(f"rule 3: {rel}:{line} imports checkout_data.repos.{repo}")
        # Rule 4: SQL construction outside checkout_data.
        if module in ("sqlalchemy", "sqlalchemy.sql") and SQL_NAMES & set(names):
            bad = ", ".join(sorted(SQL_NAMES & set(names)))
            out.append(f"rule 4: {rel}:{line} imports SQL ({bad}) from {module}")
    return out


def _strings(tree: ast.AST) -> list[tuple[str, int]]:
    return [
        (n.value, n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _check_repo(svc: str, path: pathlib.Path, rel: str) -> list[str]:
    """Rule 5: SQL in repos/<svc>.py against the ownership registry."""
    out = []
    for sql, line in _strings(ast.parse(path.read_text(), str(path))):
        for table in _WRITE.findall(sql):
            if table in TABLES and TABLES[table].owner != svc:
                out.append(f"rule 5: {rel}:{line} {svc} writes {table} (owner: {TABLES[table].owner})")
        for table in _READ.findall(sql):
            if table in TABLES and svc not in TABLES[table].readers:
                out.append(f"rule 5: {rel}:{line} {svc} reads {table} (not a declared reader)")
    return out


def _check_data_layer(path: pathlib.Path, rel: str) -> list[str]:
    """Rule 7: checkout_data stays free of the web layer."""
    out = []
    tree = ast.parse(path.read_text(), str(path))
    for module, _, line in _imports(tree):
        if _is_module(module, "fastapi") or _is_module(module, "checkout_web"):
            out.append(f"rule 7: {rel}:{line} checkout_data imports {module}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "ApiError":
                out.append(f"rule 7: {rel}:{node.lineno} checkout_data raises ApiError")
    return out


def _compose_environments(text: str) -> dict[str, list[tuple[str, int]]]:
    """{service: [(env line, lineno)]} from compose.yaml, without a YAML parser.

    Relies on the file's 2-space layout: services at indent 2, `environment:` at
    indent 4, entries at indent 6.
    """
    envs: dict[str, list[tuple[str, int]]] = {}
    in_services, service, in_env = False, None, False
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_services, service, in_env = raw.startswith("services:"), None, False
        elif in_services and indent == 2:
            service, in_env = raw.strip().rstrip(":"), False
        elif service and indent == 4:
            in_env = raw.strip() == "environment:"
        elif service and in_env and indent >= 6:
            envs.setdefault(service, []).append((raw.strip(), n))
    return envs


def _check_compose(path: pathlib.Path, rel: str) -> list[str]:
    """Rule 6: no domain service is configured with another service's address."""
    out = []
    for service, entries in _compose_environments(path.read_text()).items():
        if service not in SERVICES:
            continue
        others = [h for h in HOSTS if h != service]
        # A hostname: after a scheme or `@`, or bare, followed by :port, / or end.
        host = re.compile(rf"(?:://|@|^|[\s=,])({'|'.join(others)})(?=:\d|/|$|\s)")
        for entry, line in entries:
            value = entry.split(":", 1)[1].strip() if ":" in entry else entry
            for match in host.finditer(value):
                out.append(f"rule 6: {rel}:{line} {service} environment names host {match.group(1)}")
    return out


def check(root: pathlib.Path) -> list[str]:
    """All boundary violations under `root` (a backend/ directory)."""
    root = pathlib.Path(root)
    violations: list[str] = []

    for svc in SERVICES:
        pkg = root / "services" / svc / "src" / f"{svc}_service"
        for path in sorted(pkg.rglob("*.py")) if pkg.is_dir() else []:
            violations += _check_service(svc, path, str(path.relative_to(root)))

    data = root / "packages" / "checkout_data" / "src" / "checkout_data"
    if data.is_dir():
        for path in sorted(data.rglob("*.py")):
            rel = str(path.relative_to(root))
            violations += _check_data_layer(path, rel)
            if path.parent.name == "repos" and path.stem in SERVICES:
                violations += _check_repo(path.stem, path, rel)

    compose = root / "compose.yaml"
    if compose.is_file():
        violations += _check_compose(compose, str(compose.relative_to(root)))
    return violations


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    violations = check(root)
    for v in violations:
        print(v)
    print(f"{len(violations)} violations (7 rules, {len(SERVICES)} services, {len(TABLES)} tables)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
