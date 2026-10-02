# Data Model: Streaming Pipeline

**Date**: 2026-10-02

---

## Data Classes

### ScanData

Represents a single scan event.

```python
@dataclass
class ScanData:
    """A single scan event."""
    sku: str          # Item SKU (e.g., "SKU-000001")
    scan_id: int      # Global scan sequence ID from transaction_item.id
    scan_count: int   # Cumulative scans of this SKU within current window (populated by AggregationFilter)
```

**Use**: 
- Emitted by IngestFilter (polls database for new scans)
- Consumed by WindowFilter (buffers into circular deque)
- Passed through AggregationFilter (enriches with scan_count)

**Constraints**:
- `sku`: 3-11 characters, format "SKU-XXXXXX"
- `scan_id`: ≥ 0, strictly increasing
- `scan_count`: ≥ 1

---

### WindowData

Represents a slide event: the current 1000-scan window with metadata.

```python
@dataclass
class WindowData:
    """The current 1000-scan window emitted on slide."""
    window_start: int        # First scan_id in window (inclusive)
    window_end: int          # Last scan_id in window (inclusive)
    scans: list[ScanData]    # Exactly 1000 ScanData objects ordered by scan_id
    slide_count: int         # How many slides have occurred (for debugging)
```

**Use**:
- Emitted by WindowFilter when 500 new scans arrive
- Consumed by AggregationFilter (aggregates scan counts by SKU)

**Constraints**:
- `window_start` ≤ `window_end`
- `len(scans)` == 1000 (or fewer if fewer than 1000 total scans)
- `scans` ordered by `scan_id` ascending

---

### AggregatedData

Represents aggregated scan counts for a single SKU within the window.

```python
@dataclass
class AggregatedData:
    """Aggregated scan count for one SKU in the window."""
    sku: str          # Item SKU
    scan_count: int   # Number of scans of this SKU in current window
```

**Use**:
- Emitted by AggregationFilter (computes from WindowData)
- Consumed by RankingFilter (collects into ranking list)

**Constraints**:
- `sku`: valid SKU format
- `scan_count`: ≥ 1

---

### RankedData

Represents the final ranked output ready for persistence.

```python
@dataclass
class RankedData:
    """Final ranked items ready for database persistence."""
    window_size: int           # Configured window size (1000)
    slide_interval: int        # Configured slide interval (500)
    window_start: int          # First scan_id in window
    window_end: int            # Last scan_id in window
    computed_at: datetime      # ISO timestamp of ranking computation
    ranking: list[dict[str, Any]]  # Top 200 items, ordered by scan_count descending
```

**Use**:
- Emitted by RankingFilter
- Consumed by OutputFilter (persists to database)

**Constraints**:
- `window_size` == 1000 (config)
- `slide_interval` == 500 (config)
- `ranking`: list of dicts with keys: `{"sku": str, "scanCount": int}`
- `len(ranking)` ≤ 200 (max depth)
- `ranking` ordered by `scanCount` descending

**Example**:
```json
{
  "window_size": 1000,
  "slide_interval": 500,
  "window_start": 0,
  "window_end": 999,
  "computed_at": "2026-10-02T15:30:45.123Z",
  "ranking": [
    {"sku": "SKU-000001", "scanCount": 132},
    {"sku": "SKU-000002", "scanCount": 65},
    ...
  ]
}
```

---

## Sentinel Values

### TERMINATE

Signals pipeline shutdown. Emitted by IngestFilter when graceful shutdown is requested.

```python
TERMINATE = object()  # Sentinel; single identity comparison
```

**Use**:
- Emitted by IngestFilter to stop ingestion
- Consumed by WindowFilter (stops processing, emits to next filter)
- Propagated through all downstream filters
- Each filter exits when it receives TERMINATE

---

## Queue Communication

### Ingest Queue (`ingest_queue`)

Carries: `ScanData | TERMINATE`

From: IngestFilter  
To: WindowFilter

**Semantics**: 
- ScanData: buffered scan event
- TERMINATE: stop ingestion; process remaining window data

---

### Window Queue (`window_queue`)

Carries: `WindowData | TERMINATE`

From: WindowFilter  
To: AggregationFilter

**Semantics**:
- WindowData: new slide with 1000-scan window
- TERMINATE: no more slides; process remaining data

---

### Aggregation Queue (`aggregation_queue`)

Carries: `AggregatedData | TERMINATE`

From: AggregationFilter  
To: RankingFilter

**Semantics**:
- AggregatedData: scan count for one SKU in current window
- TERMINATE: window processing complete; finalize ranking

---

### Ranking Queue (`ranking_queue`)

Carries: `RankedData | TERMINATE`

From: RankingFilter  
To: OutputFilter

**Semantics**:
- RankedData: computed ranking ready for persistence
- TERMINATE: no more rankings; exit after final commit

---

## Schema Changes

### popular_window_snapshot (existing table, reused)

The RankedData snapshot is persisted to `popular_window_snapshot`:

```sql
CREATE TABLE popular_window_snapshot (
    id BIGSERIAL PRIMARY KEY,
    window_size INT NOT NULL,
    slide_interval INT NOT NULL,
    window_start BIGINT NOT NULL,
    window_end BIGINT NOT NULL,
    ranking JSONB NOT NULL,  -- Array of {sku, scanCount}
    computed_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Index**: Single row (upsert by `window_end`):
```sql
CREATE UNIQUE INDEX idx_popular_latest ON popular_window_snapshot (id) 
WHERE id = (SELECT MAX(id) FROM popular_window_snapshot);
```

Or use `DELETE + INSERT` for simplicity (current approach in Spec 003).

---

## Type Annotations

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Any

@dataclass
class ScanData:
    sku: str
    scan_id: int
    scan_count: int = 0  # Default; populated by downstream filter

@dataclass
class WindowData:
    window_start: int
    window_end: int
    scans: list[ScanData]
    slide_count: int = 0

@dataclass
class AggregatedData:
    sku: str
    scan_count: int

@dataclass
class RankedData:
    window_size: int
    slide_interval: int
    window_start: int
    window_end: int
    computed_at: datetime
    ranking: list[dict[str, Any]]
```

---

## Memory Footprint

### IngestFilter Window Buffer

```
1000 scans × ~80 bytes per ScanData ≈ 80 KB
```

**Breakdown**:
- sku: ~20 bytes (string interning, so mostly references)
- scan_id: 8 bytes (int)
- scan_count: 8 bytes (int)
- Python object overhead: ~50 bytes per ScanData

**Conclusion**: ~80 KB for 1000-scan window is negligible.

---

## Comparison with Spec 003

| Aspect | Spec 003 | Spec 004 |
|:---|:---|:---|
| Data flow | SQL rows → AggregatedData | ScanData → WindowData → AggregatedData |
| Window source | Database query | In-memory deque |
| Window timing | Query-driven | Event-driven (every 500 scans) |
| Latency | 1 SQL round-trip per slide | In-memory operations (microseconds) |
| Complexity | Simpler (fewer types) | More types, clearer semantics |

---

## Notes

- All dataclasses are immutable (frozen=True recommended for queue safety)
- Sentinel value TERMINATE is a single object for identity comparison (`if msg is TERMINATE`)
- Window metadata (window_start, window_end) allows API to track progression
- RankedData includes `computed_at` timestamp for sorting/debugging
