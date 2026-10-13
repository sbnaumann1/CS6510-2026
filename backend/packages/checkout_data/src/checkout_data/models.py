"""SQLAlchemy ORM models — the schema source of truth.

Mirrors the DDL in specs/001-checkout-backend/data-model.md. All money is integer
cents (research R4); all timestamps are TIMESTAMPTZ.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TxStatus(str, enum.Enum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


tx_status_enum = Enum(
    TxStatus,
    name="tx_status",
    values_callable=lambda e: [m.value for m in e],
    create_type=True,
)


class CatalogItem(Base):
    __tablename__ = "catalog_item"

    sku: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("price_cents > 0", name="catalog_item_price_cents_check"),
    )


class InventoryStock(Base):
    __tablename__ = "inventory_stock"

    sku: Mapped[str] = mapped_column(
        Text, ForeignKey("catalog_item.sku"), primary_key=True
    )
    current_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_stock: Mapped[int] = mapped_column(Integer, nullable=False)

    # Backstop only — the conditional UPDATE in the completion path (R10) is what
    # actually prevents negative stock. If this fires, a concurrency bug exists.
    __table_args__ = (
        CheckConstraint("current_stock >= 0", name="inventory_stock_non_negative"),
    )


class Transaction(Base):
    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    station_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TxStatus] = mapped_column(
        tx_status_enum, nullable=False, server_default=TxStatus.OPEN.value
    )
    # Denormalized counters maintained by the scan CTE (R9) so ScanResult needs
    # no aggregation.
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    running_total_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    total_amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Partial index for the abandonment sweeper — keeps millions of COMPLETED
    # rows out of the index.
    __table_args__ = (
        Index(
            "transaction_open_started_idx",
            "started_at",
            postgresql_where=text("status = 'OPEN'"),
        ),
    )


class TransactionItem(Base):
    __tablename__ = "transaction_item"

    # This identity column IS the global scan sequence feeding the popular-items
    # window (R6); windowStart/windowEnd are values of this column.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transaction.id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(Text, ForeignKey("catalog_item.sku"), nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("transaction_item_tx_idx", "transaction_id"),
        # For verify_invariant.py's per-SKU aggregate, not the hot path.
        Index("transaction_item_sku_idx", "sku"),
    )


class LowStockAlert(Base):
    __tablename__ = "low_stock_alert"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    sku: Mapped[str] = mapped_column(Text, ForeignKey("catalog_item.sku"), nullable=False)
    current_stock: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    triggered_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("low_stock_alert_sku_time_idx", "sku", text("triggered_at DESC")),
    )


class PopularWindowSnapshot(Base):
    __tablename__ = "popular_window_snapshot"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default="1")
    window_size: Mapped[int] = mapped_column(Integer, nullable=False)
    slide_interval: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    window_end: Mapped[int] = mapped_column(BigInteger, nullable=False)
    computed_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False)
    # [{"sku": "...", "scanCount": 123}, ...] — rank is index + 1. Top 200 only.
    ranking: Mapped[list] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        CheckConstraint("id = 1", name="popular_window_snapshot_single_row"),
    )
