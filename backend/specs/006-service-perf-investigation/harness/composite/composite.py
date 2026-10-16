"""All four 005 service apps in one ASGI process, routed like nginx, with lifespans."""
from contextlib import AsyncExitStack
import pathlib
import sys

# backend/tests, for the gateway route table (_dispatch.py); parents[4] is backend/.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4] / "tests"))
from _dispatch import route  # noqa: E402

from analytics_service.main import app as analytics_app  # noqa: E402
from catalog_service.main import app as catalog_app  # noqa: E402
from checkout_service.main import app as checkout_app  # noqa: E402
from inventory_service.main import app as inventory_app  # noqa: E402

APPS = {"catalog": catalog_app, "checkout": checkout_app,
        "inventory": inventory_app, "analytics": analytics_app}


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        await receive()
        stack = AsyncExitStack()
        for a in APPS.values():
            await stack.enter_async_context(a.router.lifespan_context(a))
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await stack.aclose()
        await send({"type": "lifespan.shutdown.complete"})
        return
    await APPS[route(scope["path"])](scope, receive, send)
