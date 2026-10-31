"""The event envelope, topic names and event types (spec 008, contracts/events.md).

Data-free: this module defines what an event looks like on the wire, not what any
service does with it. Payload fields are documented in contracts/events.md and checked
against events/schemas/*.json by tests/unit/test_events.py (not on the hot path).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import orjson

from checkout_web.request import current_request_id

# --- topics -----------------------------------------------------------------------------

CATALOG_ITEMS = "catalog.items.v1"
CHECKOUT_SCANS = "checkout.scans.v1"
CHECKOUT_COMPLETIONS = "checkout.completions.v1"
INVENTORY_STOCK = "inventory.stock.v1"
DEAD_LETTERS = "dead-letters.v1"


@dataclass(frozen=True)
class TopicSpec:
    partitions: int
    compact: bool = False


# Mirrors events/topics.yaml (the broker's own manifest); tests assert the two agree.
TOPICS: dict[str, TopicSpec] = {
    CATALOG_ITEMS: TopicSpec(1, compact=True),
    CHECKOUT_SCANS: TopicSpec(1),
    CHECKOUT_COMPLETIONS: TopicSpec(6),
    INVENTORY_STOCK: TopicSpec(6),
    DEAD_LETTERS: TopicSpec(1),
}

# --- event types ------------------------------------------------------------------------

ITEM_PUBLISHED = "catalog.item-published"
CATALOG_PUBLISHED = "catalog.catalog-published"
SCAN_RECORDED = "checkout.scan-recorded"
SCAN_LOG_RESET = "checkout.scan-log-reset"
STOCK_RESERVATION_REQUESTED = "checkout.stock-reservation-requested"
STOCK_RELEASE_REQUESTED = "checkout.stock-release-requested"
STOCK_RESERVED = "inventory.stock-reserved"
STOCK_REJECTED = "inventory.stock-rejected"
STOCK_RELEASED = "inventory.stock-released"
RESERVATION_REFUSED = "inventory.reservation-refused"
DEAD_LETTER = "dead-letter"

EVENT_TYPES = (
    ITEM_PUBLISHED,
    CATALOG_PUBLISHED,
    SCAN_RECORDED,
    SCAN_LOG_RESET,
    STOCK_RESERVATION_REQUESTED,
    STOCK_RELEASE_REQUESTED,
    STOCK_RESERVED,
    STOCK_REJECTED,
    STOCK_RELEASED,
    RESERVATION_REFUSED,
    DEAD_LETTER,
)

MARKER_KEY = "__marker__"
CURRENT_VERSION = 1
SUPPORTED_MAJOR = 1


class EventDecodeError(ValueError):
    """Bytes that are not a valid event, or an event of a version this code cannot read."""


@dataclass(frozen=True)
class Event:
    type: str
    producer: str
    data: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: int = CURRENT_VERSION
    correlation_id: str | None = None
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "type": self.type,
            "version": self.version,
            "correlationId": self.correlation_id,
            "occurredAt": self.occurred_at,
            "producer": self.producer,
            "data": self.data,
        }


def make_event(
    type: str,
    producer: str,
    data: dict[str, Any],
    *,
    correlation_id: str | None = None,
    event_id: str | None = None,
) -> Event:
    """Build an event. The correlation id defaults to the request being handled."""
    kwargs: dict[str, Any] = {}
    if event_id is not None:
        kwargs["event_id"] = event_id
    return Event(
        type=type,
        producer=producer,
        data=data,
        correlation_id=correlation_id if correlation_id is not None else current_request_id(),
        **kwargs,
    )


def encode(event: Event) -> bytes:
    return orjson.dumps(event.to_dict())


def decode(raw: bytes | str) -> Event:
    try:
        body = orjson.loads(raw)
    except Exception as exc:
        raise EventDecodeError(f"not JSON: {exc}") from None
    return from_dict(body)


def from_dict(body: Any) -> Event:
    """Build an Event from its wire form (a decoded JSON object, or an outbox payload)."""
    if not isinstance(body, dict):
        raise EventDecodeError("an event must be a JSON object")
    try:
        version = body["version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise EventDecodeError("'version' must be a positive integer")
        if version > SUPPORTED_MAJOR:
            raise EventDecodeError(f"unsupported event version {version}")
        data = body["data"]
        if not isinstance(data, dict):
            raise EventDecodeError("'data' must be an object")
        return Event(
            type=str(body["type"]),
            producer=str(body["producer"]),
            data=data,
            event_id=str(body["eventId"]),
            version=version,
            correlation_id=body.get("correlationId"),
            occurred_at=str(body["occurredAt"]),
        )
    except KeyError as exc:
        raise EventDecodeError(f"missing field {exc}") from None


def headers(event: Event) -> dict[str, str]:
    """Kafka headers: the correlation id and the type, readable without parsing the body."""
    out = {"x-event-type": event.type}
    if event.correlation_id:
        out["x-request-id"] = event.correlation_id
    return out
