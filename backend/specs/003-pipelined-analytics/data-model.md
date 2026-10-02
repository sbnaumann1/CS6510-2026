# Data Model: Pipelined Analytics Architecture

**Phase 1 Output**: Data entities and schemas for pipeline implementation

**Date**: 2026-10-02

---

## Pipeline Data Flow

```
Database (transaction_item) 
    ↓
IngestFilter → AggregatedData 
    ↓
AggregationFilter → AggregatedData 
    ↓
RankingFilter → RankedData 
    ↓
OutputFilter → Database (popular_window_snapshot)
```

---

## Entity Definitions

### AggregatedData

**Purpose**: Represents scanned item counts within a window. Flows from IngestFilter through AggregationFilter to RankingFilter.

**Fields**:
- `sku` (str): Stock Keeping Unit identifier (e.g., "SKU-000001")
- `scan_count` (int): Number of scans for this SKU within the window bounds

**Properties**:
- Immutable (treated as value object)
- Created by IngestFilter from database query results
- Ordered by scan_count descending (top items first)

**Example**:
```python
AggregatedData(sku="SKU-000001", scan_count=132)
```

---

### RankedData

**Purpose**: Final output from RankingFilter containing ranked items with window metadata. Consumed by OutputFilter for database persistence.

**Fields**:
- `window_size` (int): Configured window size in scans (e.g., 1000)
- `slide_interval` (int): Configured slide interval in scans (e.g., 500)
- `window_start` (int): Exclusive lower bound of window (first scanned sequence exclusive)
- `window_end` (int): Inclusive upper bound of window (max scan sequence, inclusive)
- `ranking` (list[dict]): List of {"sku": str, "scanCount": int} for top 200 items

**Constraints**:
- `0 <= window_start < window_end`
- `len(ranking) <= 200`
- Ranking is ordered by scanCount descending

**State Transitions**: Created once per pipeline execution; persisted by OutputFilter; consumed by popular items API reader.

**Example**:
```python
RankedData(
    window_size=1000,
    slide_interval=500,
    window_start=8000,
    window_end=9000,
    ranking=[
        {"sku": "SKU-000001", "scanCount": 132},
        {"sku": "SKU-000002", "scanCount": 65},
        ...
    ]
)
```

---

## Database Entities (Existing)

### transaction_item table

**Used by**: IngestFilter (read-only)

**Relevant columns**:
- `id` (bigint, primary key): Scan sequence number (auto-incrementing)
- `sku` (text): SKU being scanned
- Other columns not used by analytics

**Query pattern**: Window counts within (window_start, window_end]

**No changes required**: Pipeline uses existing `db.analytics_repo.window_counts()` query.

---

### popular_window_snapshot table

**Used by**: OutputFilter (write via upsert)

**Relevant columns**:
- `id` (int, primary key): Always 1 (single snapshot row)
- `window_size` (int): Configured window size
- `slide_interval` (int): Configured slide interval
- `window_start` (bigint): Start of current window
- `window_end` (bigint): End of current window
- `computed_at` (timestamp): When this snapshot was computed
- `ranking` (jsonb): JSON array of {"sku": ..., "scanCount": ...}

**Update pattern**: Upsert (ON CONFLICT DO UPDATE) to replace entire snapshot atomically

**No changes required**: Pipeline uses existing `db.analytics_repo.upsert_snapshot()` function.

---

## Inter-Filter Communication Contract

### Queue 1: IngestFilter → AggregationFilter

**Content**: AggregatedData objects, terminated by None

**Flow**:
1. IngestFilter reads window_counts from database
2. For each row, create AggregatedData(sku, scan_count)
3. Put to queue
4. After all rows, put None (end-of-stream marker)

**Cardinality**: 0 to 200 items (RANKING_DEPTH)

---

### Queue 2: AggregationFilter → RankingFilter

**Content**: AggregatedData objects, terminated by None

**Flow**:
1. AggregationFilter reads from input queue
2. For each AggregatedData, put to output queue
3. When None received, put None (propagate end-of-stream)

**Cardinality**: Same as Queue 1 (0 to 200 items)

---

### Queue 3: RankingFilter → OutputFilter

**Content**: RankedData object, terminated by None

**Flow**:
1. RankingFilter reads all AggregatedData from input queue
2. Accumulate into list, maintaining order by scanCount
3. Create RankedData with window metadata and ranked list
4. Put RankedData to output queue
5. Put None (end-of-stream marker)

**Cardinality**: Exactly 1 RankedData per pipeline execution, followed by None

---

## Type Definitions (Python)

### AggregatedData (dataclass)

```python
from dataclasses import dataclass

@dataclass
class AggregatedData:
    sku: str
    scan_count: int
```

### RankedData (dataclass)

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class RankedData:
    window_size: int
    slide_interval: int
    window_start: int
    window_end: int
    ranking: list[dict[str, Any]]
```

---

## Validation Rules

### AggregatedData

- `sku`: Non-empty string, typically "SKU-XXXXXX" format (not validated, passed through)
- `scan_count`: Positive integer > 0 (implicit from query: COUNT(*) >= 1)

### RankedData

- `window_size`: Positive integer (comes from settings.popular_window_size)
- `slide_interval`: Positive integer (comes from settings.popular_slide_interval)
- `window_start`: Non-negative integer >= 0
- `window_end`: Positive integer, must satisfy `window_start < window_end`
- `ranking`: List of dicts with keys "sku" and "scanCount"; length <= 200
- No duplicate SKUs in ranking (enforced by database query)

---

## Persistence Semantics

**Snapshot**: The popular_window_snapshot table holds a single row (id=1) representing the most recent computation.

**Atomicity**: OutputFilter uses ON CONFLICT DO UPDATE to ensure atomic replacement of the entire snapshot. Readers (popular items API) always see a consistent snapshot.

**Versioning**: No explicit versioning; timestamp (computed_at) provides audit trail.

**No historical tracking**: Old snapshots are discarded. Only the latest ranking is stored.
