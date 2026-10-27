# Contract: Topics and Events

The only path between microservices. Machine-readable twin: `events/topics.yaml` (broker topology
and declared producers/consumers) and `events/schemas/*.json` (envelope and payload schemas).
`scripts/check_boundaries.py` verifies code against `topics.yaml`
([service-boundaries.md](./service-boundaries.md)).

## Topics

| Topic | Producer (only) | Consumers | Key | Partitions | Retention |
| --- | --- | --- | --- | --- | --- |
| `catalog.items.v1` | Catalog | Checkout, Inventory, Analytics | SKU (`__marker__` for the marker) | 1 | compacted, forever |
| `checkout.scans.v1` | Checkout | Analytics | none | 1 | 7 days |
| `checkout.completions.v1` | Checkout | Inventory | transaction id | 6 | 1 day |
| `inventory.stock.v1` | Inventory | Checkout (every worker, unshared) | transaction id | 6 | 1 day |
| `dead-letters.v1` | any consumer | operators | source topic | 1 | 14 days |

Each topic has exactly one owning producer service (except the dead-letter topic). A service's
`events.py` lists the topics it produces and consumes; the checker compares those lists with this
table.

Consumer groups: `inventory` (on `checkout.completions.v1`), `analytics` (on `checkout.scans.v1`),
and per-process, group-less readers for `catalog.items.v1` (all three services, from the beginning)
and `inventory.stock.v1` (Checkout reply consumers, from the end).

## Envelope (every event)

```json
{
  "eventId": "0b9f6f1e-6b9e-4c0e-9a35-0b0b4a7f9f10",
  "type": "inventory.stock-reserved",
  "version": 1,
  "correlationId": "req-7f3c2a",
  "occurredAt": "2026-10-26T14:03:11.482Z",
  "producer": "inventory",
  "data": { }
}
```

| Field | Rule |
| --- | --- |
| `eventId` | UUID, unique per event; republishing the same fact from an outbox keeps the same id |
| `type` | `<producer>.<fact>`; names a fact in the past tense |
| `version` | integer; consumers ignore unknown fields and dead-letter unknown major versions |
| `correlationId` | the originating request's id (also Kafka header `x-request-id`) |
| `occurredAt` | UTC, millisecond precision |
| `producer` | the owning service |
| `data` | the payload below |

Kafka message key = the ordering key in the table above. Kafka headers: `x-request-id`,
`x-event-type`.

## Catalog events (`catalog.items.v1`)

Published in this order at each Catalog startup, retried until the broker accepts them.

**`catalog.item-published`**, key = SKU

```json
{ "sku": "SKU-00042", "name": "Oat Milk 1L", "priceCents": 429 }
```

**`catalog.catalog-published`**, key = `__marker__`, published after every item

```json
{ "publishId": "b7a1…", "itemCount": 2000 }
```

A consumer builds a snapshot from items and is **ready** when it has read a marker and holds
`itemCount` distinct SKUs. A later marker with a different count replaces the snapshot's expected
count; the snapshot always overwrites by SKU.

## Scan events (`checkout.scans.v1`)

**`checkout.scan-recorded`**, no key. Published by the relay in `scanId` order.

```json
{ "scanId": 120431, "transactionId": "tx-9017", "sku": "SKU-00042" }
```

Analytics applies a scan only if `scanId` is greater than the highest it has applied; otherwise it
is a duplicate.

**`checkout.scan-log-reset`**, no key. Published when the relay detects Checkout's scan log was
replaced (the `scan_log` token changed). Analytics clears its window and its highest-applied id.

```json
{ "logId": "5d1e…" }
```

## Completion events

### `checkout.completions.v1` (Checkout → Inventory), key = transaction id

**`checkout.stock-reservation-requested`**

```json
{
  "transactionId": "tx-9017",
  "epoch": 1,
  "lines": [ { "sku": "SKU-00042", "quantity": 2 }, { "sku": "SKU-00317", "quantity": 1 } ]
}
```

Lines are merged by SKU and sorted by SKU (007 rule), so concurrent completions lock rows in one
order.

**`checkout.stock-release-requested`**

```json
{ "transactionId": "tx-9017", "epoch": 1 }
```

### `inventory.stock.v1` (Inventory → Checkout), key = transaction id

Each outcome echoes `transactionId` and `epoch` and the id of the request it answers.

**`inventory.stock-reserved`**: stock taken, or a replay of the recorded success.

```json
{ "transactionId": "tx-9017", "epoch": 1, "inReplyTo": "<eventId>", "replayed": false }
```

**`inventory.stock-rejected`**: insufficient stock; nothing changed.

```json
{ "transactionId": "tx-9017", "epoch": 1, "inReplyTo": "<eventId>", "shortSkus": ["SKU-00042"] }
```

**`inventory.stock-released`**: the reservation of that epoch is undone, or there was nothing to
undo and a refusal is now recorded.

```json
{ "transactionId": "tx-9017", "epoch": 1, "inReplyTo": "<eventId>", "unitsRestored": 3, "alreadyReleased": false }
```

**`inventory.reservation-refused`**: a release for that `(transaction, epoch)` was recorded first,
so the reservation is refused (007's `RESERVATION_RELEASED`).

```json
{ "transactionId": "tx-9017", "epoch": 1, "inReplyTo": "<eventId>" }
```

## Dead letters (`dead-letters.v1`)

**`dead-letter`**, key = source topic

```json
{
  "sourceTopic": "checkout.completions.v1", "partition": 3, "offset": 88121,
  "consumer": "inventory", "attempts": 5, "error": "ValidationError: …",
  "event": { "…the original envelope or raw text…" }
}
```

## Guarantees and rules

| Rule | Mechanism |
| --- | --- |
| Ordering | one key → one partition; one producer relays scans in id order into one partition |
| Delivery | at-least-once; producers are idempotent; consumers commit after the local transaction |
| No lost events across a crash | outbox row (or scan row) committed with the state change; sweeper/relay republishes |
| Duplicate-safe | every handler is keyed by `(transaction, epoch)`, `scanId` or SKU |
| Re-ask, same answer | a repeated request re-emits the recorded outcome |
| Poison events | five attempts, then `dead-letters.v1`, offset committed |
| Evolution | add optional fields only; breaking changes need a new `type` or `version` |
