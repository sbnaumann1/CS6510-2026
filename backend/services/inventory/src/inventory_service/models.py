"""Inventory's schema (data-model.md §3). No foreign key reaches another service's data."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class InventoryStock(Base):
    __tablename__ = "inventory_stock"

    sku: Mapped[str] = mapped_column(Text, primary_key=True)
    current_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_stock: Mapped[int] = mapped_column(Integer, nullable=False)

    # Backstop only: the conditional statement in reserve.py is what prevents negative
    # stock. If this fires, a concurrency bug exists.
    __table_args__ = (
        CheckConstraint("current_stock >= 0", name="inventory_stock_non_negative"),
    )


class StockAdjustment(Base):
    """The idempotency record for one transaction's reservation.

    `RELEASED` with empty `lines` and no earlier `APPLIED` is a tombstone: it makes a
    reserve that arrives after its release refuse instead of apply.
    """

    __tablename__ = "stock_adjustment"

    transaction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    lines: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("state IN ('APPLIED', 'RELEASED')", name="stock_adjustment_state"),
    )


class LowStockAlert(Base):
    __tablename__ = "low_stock_alert"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    current_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    triggered_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("low_stock_alert_sku_time_idx", "sku", text("triggered_at DESC")),
    )
