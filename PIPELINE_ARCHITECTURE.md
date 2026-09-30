# Pipelined Analytics Architecture

## Overview

The popular items windowed analytics functionality has been refactored into a multi-stage pipeline architecture. The system decomposes the analytics computation into independent, concurrent filters connected by asyncio queues for asynchronous inter-filter communication.

## Pipeline Stages

### 1. **IngestFilter**
- **Purpose**: Reads scan data from the database within the window bounds
- **Input**: Window start and end sequence numbers
- **Output**: `AggregatedData` objects representing scan counts per SKU
- **Implementation**: Fetches window_counts from the analytics repository and emits each row to the output queue

### 2. **AggregationFilter**
- **Purpose**: Buffers and forwards aggregated scan count data
- **Input**: `AggregatedData` from IngestFilter
- **Output**: `AggregatedData` forwarded to RankingFilter
- **Implementation**: Acts as a pass-through stage that maintains queue isolation between stages

### 3. **RankingFilter**
- **Purpose**: Ranks items by scan count and prepares final output
- **Input**: `AggregatedData` from AggregationFilter
- **Output**: `RankedData` containing ranked items with window metadata
- **Implementation**: Accumulates aggregated data, sorts/ranks, and emits the final ranked list

### 4. **OutputFilter**
- **Purpose**: Persists the final snapshot to the database
- **Input**: `RankedData` from RankingFilter
- **Output**: None (side effect only - writes to DB)
- **Implementation**: Converts RankedData to JSON and upserts the popular_window_snapshot table

## Inter-Filter Communication

Filters communicate through asyncio queues:
```
IngestFilter → [Queue 1] → AggregationFilter → [Queue 2] → RankingFilter → [Queue 3] → OutputFilter
```

Each filter:
- Reads from its input queue (blocks if empty)
- Processes the data
- Writes to its output queue
- Sends `None` sentinel value to signal end-of-stream

This design enables:
- **Asynchronous execution**: Filters run concurrently, processing different items independently
- **Backpressure handling**: Queue sizes naturally limit memory usage
- **Modularity**: Each filter has a single responsibility
- **Testability**: Filters can be tested in isolation

## Data Model

### AggregatedData
```python
@dataclass
class AggregatedData:
    sku: str          # Stock Keeping Unit
    scan_count: int   # Count of scans within window
```

### RankedData
```python
@dataclass
class RankedData:
    window_size: int        # Total window size (scans)
    slide_interval: int     # Slide interval for hopping window
    window_start: int       # Window start sequence (exclusive)
    window_end: int         # Window end sequence (inclusive)
    ranking: list[dict]     # List of {"sku": "...", "scanCount": N}
```

## Integration

The pipeline is invoked through `pipeline.recompute_windowed()` which:
1. Computes the current window bounds based on the max scan sequence
2. Creates asyncio queues for inter-filter communication
3. Instantiates each filter with its configuration
4. Runs all filters concurrently using `asyncio.gather()`

This replaces the previous direct call to `popular_items.recompute()` in the scheduler.

## Performance Characteristics

- **Latency**: Filters process items as soon as they arrive from previous stages
- **Throughput**: Limited by the slowest stage; queues provide buffering
- **Memory**: Queue-based communication avoids materializing entire datasets
- **Scalability**: Can add more stages or enhance existing stages without changing interface

## Testing

The pipeline was tested with the load client under two scenarios:

### Normal Load Test
- 10 stations, 60 seconds duration
- Results: report-20260929-202454.json
- Popular items ranking generated correctly

### Stress Test
- 100 stations, 120 seconds duration
- Results: report-YYYYMMDD-HHMMSS.json
- Tests pipeline under high concurrent load
