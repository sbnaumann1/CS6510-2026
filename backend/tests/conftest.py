"""Shared test fixtures.

Tests run against a real PostgreSQL database — the correctness properties under
test (row locks, conditional decrement, advisory locks) do not exist in SQLite.

They run against their OWN database (`checkout_test` by default), never the one
the server uses. `seed.py --reset` does `DROP SCHEMA public CASCADE`, so sharing
would let a test run destroy a live server's schema mid-flight — the server keeps
serving its cached catalog while every write fails with a 500. Override with
TEST_DATABASE_URL if you need somewhere else.

By default the four service apps run in-process behind tests/_dispatch.py (the
gateway's route table). With GATEWAY_URL set, the same tests run over real HTTP
against the compose test stack (specs/005-service-architecture/quickstart.md V3).
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import psycopg2
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.engine import make_url

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A small catalog keeps the per-test reset fast; the formulas are identical.
os.environ.setdefault("CATALOG_SIZE", "50")
os.environ.setdefault("STOCK_PER_ITEM", "100")

_DEFAULT_DSN = "postgresql+asyncpg:///checkout?host=/tmp"


def _query_host(url) -> str | None:
    host = url.query.get("host")
    if isinstance(host, (list, tuple)):
        return host[0] if host else None
    return host


def _ensure_database(dsn: str) -> None:
    """Create the test database if it does not exist yet."""
    url = make_url(dsn)
    params: dict[str, object] = {"dbname": "postgres"}
    host = url.host or _query_host(url)
    if host:
        params["host"] = host
    if url.port:
        params["port"] = url.port
    if url.username:
        params["user"] = url.username
    if url.password:
        params["password"] = url.password

    conn = psycopg2.connect(**params)  # type: ignore[arg-type]
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE DATABASE "{url.database}"')
    finally:
        conn.close()


def _resolve_test_dsn() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(os.environ.get("DATABASE_URL", _DEFAULT_DSN))
    return url.set(database=f"{url.database or 'checkout'}_test").render_as_string(
        hide_password=False
    )


_TEST_DSN = _resolve_test_dsn()

# Safety net: never let a stray DATABASE_URL point the suite at real data.
# seed.py --reset would drop its schema.
if not (make_url(_TEST_DSN).database or "").endswith("_test"):
    raise RuntimeError(
        f"Refusing to run tests against {make_url(_TEST_DSN).database!r}: "
        "the test database name must end in '_test'."
    )

_ensure_database(_TEST_DSN)
# Set before importing checkout_data — its settings and engine are bound at
# import time, and load_dotenv() does not override an existing variable.
os.environ["DATABASE_URL"] = _TEST_DSN

# Gateway mode (005 research R11): the same tests over real HTTP against the
# compose test stack. That stack is already seeded by its own `seed` step and its
# services hold open connections, so the session never drops the schema there.
GATEWAY_URL = os.environ.get("GATEWAY_URL")

from checkout_data import SessionLocal, engine  # noqa: E402
from checkout_data.config import settings  # noqa: E402
from tests._dispatch import build_dispatch_app  # noqa: E402


def _seed(reset: bool = True) -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / "seed.py")]
    if reset:
        cmd.append("--reset")
    subprocess.run(cmd, check=True, capture_output=True, cwd=ROOT)


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Create the schema once for the whole session."""
    _seed(reset=GATEWAY_URL is None)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engine_per_loop():
    """pytest-asyncio gives each test a fresh event loop; pooled asyncpg
    connections are bound to the loop that created them, so the pool must not
    survive across tests."""
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def reset_db() -> None:
    """Reset catalog, stock and run state before a test that needs a clean slate."""
    _seed(reset=False)


async def load_service_caches() -> None:
    """Do what each service's lifespan does at startup (lifespans do not run
    under ASGITransport): load the per-process catalog cache and pre-render the
    catalog service's /items body."""
    from catalog_service.main import load

    await load()


@pytest_asyncio.fixture
async def client(reset_db) -> AsyncClient:
    if GATEWAY_URL:
        async with AsyncClient(base_url=GATEWAY_URL, timeout=15) as c:
            yield c
        return
    async with AsyncClient(app=build_dispatch_app(), base_url="http://test") as c:
        await load_service_caches()
        yield c


@pytest_asyncio.fixture
async def run_pipeline_until():
    """Run the persistent analytics pipeline until it has published a window
    ending at or after `end`, then stop it gracefully."""
    import asyncio

    from analytics_service.supervisor import PipelineSupervisor
    from checkout_data.repos import analytics as analytics_repo

    async def run(end: int, timeout: float = 10.0) -> None:
        sup = PipelineSupervisor()
        task = asyncio.create_task(sup.run())

        async def published() -> None:
            while True:
                async with SessionLocal() as s:
                    row = await analytics_repo.read_snapshot(s)
                if row is not None and row.window_end >= end and sup.instances:
                    return
                await asyncio.sleep(0.02)

        try:
            await asyncio.wait_for(published(), timeout)
        finally:
            await sup.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return run


@pytest_asyncio.fixture
async def session():
    async with SessionLocal() as s:
        yield s


@pytest.fixture(scope="session")
def catalog_size() -> int:
    return settings.catalog_size


@pytest.fixture(scope="session")
def stock_per_item() -> int:
    return settings.stock_per_item
