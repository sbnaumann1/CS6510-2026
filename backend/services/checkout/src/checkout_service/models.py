"""Checkout's schema (data-model.md §2).

All money is integer cents (001 research R4); all timestamps are TIMESTAMPTZ. No foreign
key reaches another service's data: `transaction_item.sku` is plain text.
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
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TxStatus(str, enum.Enum):
    OPEN = "OPEN"
    COMPLETING = "COMPLETING"  # internal; the public API reports it as OPEN
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


tx_status_enum = Enum(
    TxStatus,
    name="tx_status",
    values_callable=lambda e: [m.value for m in e],
    create_type=True,
)


class CompletionState(str, enum.Enum):
    RESERVING = "RESERVING"
    RESERVED = "RESERVED"
    COMPLETED = "COMPLETED"
    COMPENSATING = "COMPENSATING"


class Transaction(Base):
    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    station_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[TxStatus] = mapped_column(
        tx_status_enum, nullable=False, server_default=TxStatus.OPEN.value
    )
    # Denormalized counters maintained by the scan CTE (001 R9) so ScanResult needs
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

    # Partial index for the abandonment sweeper: keeps millions of COMPLETED rows out
    # of the index.
    __table_args__ = (
        Index(
            "transaction_open_started_idx",
            "started_at",
            postgresql_where=text("status = 'OPEN'"),
        ),
    )


class TransactionItem(Base):
    __tablename__ = "transaction_item"

    # This identity column IS the global scan sequence that Analytics reads through
    # GET /internal/scans; windowStart/windowEnd are values of this column.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transaction.id"), nullable=False
    )
    sku: Mapped[str] = mapped_column(Text, nullable=False)
    unit_price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    scanned_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("transaction_item_tx_idx", "transaction_id"),
        # For the per-SKU sold report, not the hot path.
        Index("transaction_item_sku_idx", "sku"),
    )


class Completion(Base):
    """One row per in-flight or finished completion saga; drives recovery."""

    __tablename__ = "completion"

    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transaction.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(Text, nullable=False)
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('RESERVING', 'RESERVED', 'COMPLETED', 'COMPENSATING')",
            name="completion_state",
        ),
        Index("completion_open_idx", "updated_at", postgresql_where=text("state <> 'COMPLETED'")),
    )
