"""Recovery loop: resume or compensate completions that a crash or lost message left
mid-saga (contracts/completion-saga.md "Recovery loop", spec 007 research R4).

Every `recovery_interval_s` each Checkout instance claims completions whose lease has
expired (bumping `updated_at` under FOR UPDATE SKIP LOCKED, so replicas do not collide),
then drives each one with the same idempotent steps the live path uses:

    RESERVING     outcome unknown   -> release, then reopen the transaction
    RESERVED      stock is reserved -> finalize (roll forward)
    COMPENSATING  already rolling back -> release, then reopen
"""

from __future__ import annotations

import asyncio
import logging

from checkout_service import repo, saga
from checkout_service.config import settings
from checkout_service.db import SessionLocal
from checkout_web.errors import UpstreamUnavailable
from checkout_web.request import log_step, new_request_id, set_request_id, _request_id

log = logging.getLogger("checkout")


async def run_once(lease_s: float | None = None) -> int:
    """One pass. Returns how many completions were driven to a terminal state."""
    lease = settings.recovery_lease_s if lease_s is None else lease_s
    async with SessionLocal() as session:
        async with session.begin():
            claimed = list(await repo.claim_stuck(session, lease))
    done = 0
    for row in claimed:
        public = saga.public_id(row.transaction_id)
        token = set_request_id(new_request_id())
        try:
            if row.state == "RESERVED":
                completed_at = await saga.finalize_step(row.transaction_id, row.total_cents)
                outcome = "finalized" if completed_at is not None else "already-done"
            else:
                await saga.rollback(row.transaction_id, reason=f"recovered from {row.state}")
                outcome = "rolled-back"
            log_step("recover", tx=public, outcome=outcome, from_state=row.state)
            done += 1
        except UpstreamUnavailable:
            log_step("recover", tx=public, outcome="retry-later", from_state=row.state)
        except Exception:
            log.exception("recovery of %s failed", public)
        finally:
            _request_id.reset(token)
    return done


async def recovery_loop() -> None:
    while True:
        await asyncio.sleep(settings.recovery_interval_s)
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("recovery pass failed")
