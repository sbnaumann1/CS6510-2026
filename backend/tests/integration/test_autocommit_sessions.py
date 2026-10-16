"""Integration test: autocommit sessions for single-statement routes (spec 006, T019).

Covers data-model.md §2: a write through AutocommitSessionLocal commits on its own
(no BEGIN/COMMIT round trips), and connections go back to the shared pool with the
default isolation level, so transactional sessions stay transactional.
"""

from __future__ import annotations

from sqlalchemy import text

from checkout_data import AutocommitSessionLocal, SessionLocal, engine

_INSERT = text("INSERT INTO transaction (station_id) VALUES (:s) RETURNING id")
_COUNT = text("SELECT count(*) FROM transaction WHERE id = :id")
_CLEANUP = text("DELETE FROM transaction WHERE station_id LIKE 'ac-probe%'")


async def _peer_sees(tx_id: int) -> int:
    async with engine.connect() as peer:
        return await peer.scalar(_COUNT, {"id": tx_id})


async def test_autocommit_write_is_visible_before_commit(reset_db):
    async with AutocommitSessionLocal() as s:
        tx_id = await s.scalar(_INSERT, {"s": "ac-probe"})
        assert await _peer_sees(tx_id) == 1  # committed without session.commit()
        await s.execute(_CLEANUP)


async def test_pool_returns_connections_transactional(reset_db):
    for _ in range(5):
        async with AutocommitSessionLocal() as s:
            await s.scalar(_INSERT, {"s": "ac-probe-auto"})

        async with SessionLocal() as s:
            tx_id = await s.scalar(_INSERT, {"s": "ac-probe-tx"})
            assert await _peer_sees(tx_id) == 0  # still inside its transaction
            await s.rollback()
        assert await _peer_sees(tx_id) == 0  # and gone after rollback

    async with AutocommitSessionLocal() as s:
        await s.execute(_CLEANUP)
