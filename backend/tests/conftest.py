"""Shared test fixtures.

Tests run against real PostgreSQL databases: the correctness properties under test (row
locks, conditional decrement, savepoints) do not exist in SQLite. Spec 007 has two
database services, so there are two test databases, each ending in `_test`:

    checkout_checkout_test    Checkout's private store
    checkout_inventory_test   Inventory's private store

Catalog's store is a CSV file in a temporary directory (CATALOG_FILE), and Analytics has no
store at all: the `analytics` fixture runs its in-memory pipeline against Checkout's scan
log. `seed --reset` drops a schema, so the suite refuses to run against a database whose
name does not end in `_test`.

By default the four service apps run in-process: the public routes behind tests/_dispatch.py
(the gateway twin) and the services' own calls to each other through its `MeshTransport`,
which can inject failures. With GATEWAY_URL set, the same contract tests run over real HTTP
against the compose test stack (specs/007-microservices-architecture/quickstart.md V5);
point CHECKOUT_TEST_DATABASE_URL and INVENTORY_TEST_DATABASE_URL at that stack's published
database ports.

Fixtures may change between specs, assertions in tests/contract may not. Two fixture-level
conveniences model the architecture rather than hide it:

* `session` routes each statement to the database that owns the table it names, so a test
  can set up stock (Inventory) and read a transaction (Checkout) through one handle.
* The `client` waits for Analytics to catch up with Checkout's scan log before a read of
  /analytics/*, because Analytics reads scans asynchronously.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys
import tempfile

import psycopg2
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# A small catalog keeps the per-test reset fast; the formulas are identical.
os.environ.setdefault("CATALOG_SIZE", "50")
os.environ.setdefault("STOCK_PER_ITEM", "100")
# Analytics tails Checkout's scan log; poll fast so tests do not wait for the 100 ms default.
os.environ.setdefault("POPULAR_POLL_MS", "10")

_DEFAULT_DSN = "postgresql+asyncpg:///checkout?host=/tmp"

# Gateway mode (quickstart V5): the same contract tests over real HTTP against compose.
GATEWAY_URL = os.environ.get("GATEWAY_URL")


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


def _resolve_dsn(service: str, debug_port: int) -> str:
    explicit = os.environ.get(f"{service.upper()}_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if GATEWAY_URL:
        return (
            f"postgresql+asyncpg://{service}:{service}@127.0.0.1:{debug_port}/{service}_test"
        )
    base = make_url(os.environ.get("DATABASE_URL", _DEFAULT_DSN))
    return base.set(database=f"checkout_{service}_test").render_as_string(hide_password=False)


_CHECKOUT_DSN = _resolve_dsn("checkout", 5442)
_INVENTORY_DSN = _resolve_dsn("inventory", 5443)

# Safety net: never let a stray DATABASE_URL point the suite at real data.
for _dsn in (_CHECKOUT_DSN, _INVENTORY_DSN):
    if not (make_url(_dsn).database or "").endswith("_test"):
        raise RuntimeError(
            f"Refusing to run tests against {make_url(_dsn).database!r}: "
            "the test database name must end in '_test'."
        )

if not GATEWAY_URL:
    _ensure_database(_CHECKOUT_DSN)
    _ensure_database(_INVENTORY_DSN)

# Set before importing any service: their settings and engines are bound at import time,
# and load_dotenv() does not override an existing variable.
os.environ["CHECKOUT_DATABASE_URL"] = _CHECKOUT_DSN
os.environ["INVENTORY_DATABASE_URL"] = _INVENTORY_DSN
_CATALOG_DIR = tempfile.mkdtemp(prefix="catalog-test-")
os.environ["CATALOG_FILE"] = str(pathlib.Path(_CATALOG_DIR) / "catalog.csv")

from analytics_service import catalog_client as analytics_catalog  # noqa: E402
from analytics_service import scan_source, state as analytics_state  # noqa: E402
from analytics_service.supervisor import PipelineSupervisor  # noqa: E402
from catalog_service import seed as catalog_seed  # noqa: E402
from catalog_service.main import load as load_catalog  # noqa: E402
from checkout_service import catalog_client as checkout_catalog  # noqa: E402
from checkout_service import inventory_client  # noqa: E402
from checkout_service import seed as checkout_seed  # noqa: E402
from checkout_service.db import SessionLocal as CheckoutSessionLocal  # noqa: E402
from checkout_service.db import engine as checkout_engine  # noqa: E402
from checkout_service.db import ping as checkout_ping  # noqa: E402
from inventory_service import catalog_client as inventory_catalog  # noqa: E402
from inventory_service import seed as inventory_seed  # noqa: E402
from inventory_service.config import settings as inventory_settings  # noqa: E402
from inventory_service.db import SessionLocal as InventorySessionLocal  # noqa: E402
from inventory_service.db import engine as inventory_engine  # noqa: E402
from inventory_service.db import ping as inventory_ping  # noqa: E402
from tests._dispatch import MeshTransport, build_apps, build_dispatch_app  # noqa: E402

_INVENTORY_TABLES = re.compile(r"\b(inventory_stock|low_stock_alert|stock_adjustment)\b", re.I)
_CHECKOUT_TABLES = re.compile(r"\b(transaction|transaction_item|completion)\b", re.I)


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Create each database's schema (and the catalog file) once for the whole session.

    In gateway mode the compose test stack already seeded itself."""
    if GATEWAY_URL:
        return
    catalog_seed.main(["--reset"])
    checkout_seed.main(["--reset"])
    inventory_seed.main(["--reset"])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_engines_per_loop():
    """pytest-asyncio gives each test a fresh event loop; pooled asyncpg connections are
    bound to the loop that created them, so the pools must not survive across tests.

    Each fresh pool is warmed with one connection first. SQLAlchemy runs a pool's
    first-connect hook under a threading lock, and when several connections are opened
    concurrently on a cold pool the waiters block the event loop thread (the services warm
    their pools in their lifespans for the same reason)."""
    if not GATEWAY_URL:
        await checkout_ping()
        await inventory_ping()
    yield
    await checkout_engine.dispose()
    await inventory_engine.dispose()


@pytest_asyncio.fixture
async def reset_db() -> None:
    """Reset every service's run state before a test that needs a clean slate."""
    if not GATEWAY_URL:
        checkout_seed.main([])
        inventory_seed.main([])
        catalog_seed.main(["--reset"])
        analytics_state.reset()


@pytest_asyncio.fixture
async def mesh(reset_db) -> MeshTransport:
    """The in-process service mesh: every service client talks through this transport.

    Tests inject failures with `mesh.fail(target, path_prefix, times=N, mode=...)`."""
    if GATEWAY_URL:
        yield None  # the real services talk to each other
        return
    apps = build_apps()
    transport = MeshTransport(apps)
    transport.apps = apps  # type: ignore[attr-defined]
    for module in (checkout_catalog, inventory_catalog, analytics_catalog, inventory_client, scan_source):
        module.reset(transport)
    load_catalog()  # a lifespan would do this; lifespans do not run under ASGITransport
    yield transport
    for module in (checkout_catalog, inventory_catalog, analytics_catalog, inventory_client, scan_source):
        module.reset(None)


class AnalyticsHandle:
    """The in-memory Analytics pipeline a test runs against Checkout's scan log."""

    def __init__(self) -> None:
        self.sup: PipelineSupervisor | None = None
        self.task: asyncio.Task | None = None

    async def start(self, **kw) -> PipelineSupervisor:
        self.sup = PipelineSupervisor(**kw)
        self.task = asyncio.create_task(self.sup.run())
        deadline = asyncio.get_running_loop().time() + 5
        while self.sup.instances < 1:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("analytics pipeline did not bootstrap")
            await asyncio.sleep(0.01)
        return self.sup

    async def stop(self) -> None:
        if self.sup is not None:
            await self.sup.stop()
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.sup = self.task = None

    async def restart(self, *, forget: bool = True, **kw) -> PipelineSupervisor:
        """Stop and start the pipeline, as a process restart would (memory is lost)."""
        await self.stop()
        if forget:
            analytics_state.reset()
        return await self.start(**kw)


@pytest_asyncio.fixture
async def analytics(mesh) -> AnalyticsHandle:
    handle = AnalyticsHandle()
    if GATEWAY_URL:
        yield handle  # the real Analytics runs in the stack
        return
    analytics_state.reset()
    await handle.start()
    yield handle
    await handle.stop()


async def _checkout_max_scan_id() -> int:
    async with CheckoutSessionLocal() as s:
        return int(await s.scalar(text("SELECT coalesce(max(id), 0) FROM transaction_item")) or 0)


async def _wait_for_analytics(timeout: float = 10.0) -> None:
    """Wait until Analytics has read every scan Checkout has committed."""
    if GATEWAY_URL:
        await asyncio.sleep(0.5)  # the real Analytics polls on its own
        return
    target = await _checkout_max_scan_id()
    deadline = asyncio.get_running_loop().time() + timeout
    while analytics_state.cursor < target:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"analytics at {analytics_state.cursor}, checkout at {target}")
        await asyncio.sleep(0.01)


class SyncingClient(AsyncClient):
    """Reads of /analytics/* wait for Analytics to catch up first (it reads scans
    asynchronously; the tests assert on content, not on timing)."""

    async def get(self, url, **kwargs):  # type: ignore[override]
        if str(url).startswith("/analytics"):
            await _wait_for_analytics()
        return await super().get(url, **kwargs)


@pytest_asyncio.fixture
async def client(reset_db, mesh, analytics) -> AsyncClient:
    if GATEWAY_URL:
        async with SyncingClient(base_url=GATEWAY_URL, timeout=15) as c:
            yield c
        return
    async with SyncingClient(app=build_dispatch_app(mesh.apps), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def checkout_client(reset_db, mesh) -> AsyncClient:
    """The public API with no Analytics pipeline running, for tests that start, stop and
    restart pipelines of their own."""
    async with AsyncClient(app=build_dispatch_app(mesh.apps), base_url="http://test") as c:
        yield c


class RoutedSession:
    """One handle over both databases: each statement goes to the store that owns the
    table it names. A statement naming tables of both is a test bug and is refused."""

    def __init__(self, checkout, inventory) -> None:
        self.checkout = checkout
        self.inventory = inventory

    def _pick(self, statement):
        sql = str(statement)
        inv, chk = bool(_INVENTORY_TABLES.search(sql)), bool(_CHECKOUT_TABLES.search(sql))
        if inv and chk:
            raise AssertionError(f"statement spans two services' tables: {sql[:120]}")
        return self.inventory if inv else self.checkout

    async def execute(self, statement, *args, **kwargs):
        return await self._pick(statement).execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        return await self._pick(statement).scalar(statement, *args, **kwargs)

    async def commit(self) -> None:
        await self.checkout.commit()
        await self.inventory.commit()

    async def rollback(self) -> None:
        await self.checkout.rollback()
        await self.inventory.rollback()


@pytest_asyncio.fixture
async def checkout_session():
    async with CheckoutSessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def inventory_session():
    async with InventorySessionLocal() as s:
        yield s


@pytest_asyncio.fixture
async def session():
    async with CheckoutSessionLocal() as c, InventorySessionLocal() as i:
        yield RoutedSession(c, i)


@pytest_asyncio.fixture
async def poll_analytics():
    """Returns a coroutine function that waits until Analytics has read all scans."""
    return _wait_for_analytics


@pytest.fixture(scope="session")
def catalog_size() -> int:
    return inventory_settings.catalog_size


@pytest.fixture(scope="session")
def stock_per_item() -> int:
    return inventory_settings.stock_per_item
