# Local log storage comparison (2026-09-29)

[**English**](log-storage-benchmark-2026-09-29.md) · [简体中文](log-storage-benchmark-2026-09-29.zh-CN.md)

## Findings

Moving all logs into SQLite for write speed was unwarranted. Batched JSONL was fastest in these real IPC measurements. Address the shared MetricsDaemon's per-record open/write/close first. SQLite's additional value is indexed persistent queries and transactions. Trace already uses SQLite, so removing its JSONL copy primarily saves space and gives little large-sample throughput improvement.

This comparison preceded the production change. The subsequently implemented batch JSONL writer is documented in [implementation and verification](batch-jsonl-implementation-2026-09-29.md).

## Existing storage paths

- `robotenon/infra/trace.py`: TraceDaemon writes both SQLite WAL and JSONL, committing once per ingest_batch. SQL queries already use indices.
- `robotenon/infra/metrics.py`: MetricsDaemon.ingest_batch called ingest for each record, opening and closing the file each time. In-memory aggregation and incremental JSONL reads already existed.
- `robotenon/runtime/observability.py`: common model/simulator logging service for all benchmarks, configured by default with a trace DB, trace JSONL, and metrics JSONL.
- Episode summaries already use `episodes.sqlite3`. The migration cannot claim that existing benefit again. Text stdout/stderr, images, and videos were outside this database comparison.

## Method and scope

Platform: macOS-27.0-arm64-arm-64bit, Python 3.11.10.

- Used 8192 and 65536 records, 3 repeats per size, reporting medians. Mode order used a fixed random shuffle.
- Synthetic data followed production TraceSpan/MetricSample structures: 128 logical episodes, counter/histogram metrics, and 512-character trace attributes. These were not replayed server logs.
- Direct writes used 1/64/256 records per batch. Real IPC used the existing IngestServer/UnixSocketMetricsSink with 128 logical producers, up to 32 active connections, and 64 records/request. This respected the existing 64-connection cap and did not simulate 128 concurrent simulators.
- Metrics prototypes retained production validation, serialization, and memory aggregation, intercepting only persistence to write at batch end. The SQLite prototype indexed episodes. Trace SQLite-only used existing constructor arguments.
- Each successful trial checked persisted metric records, count, order or timestamp-sorted contents, and aggregates. Trace checks covered total count, latest attributes, and episode query count. Concurrent reordering was permitted, loss/duplication were not.
- JSONL uses flush without fsync. SQLite WAL FULL and NORMAL were measured separately. They provide different durability tradeoffs. Power-loss and process-crash recovery were not tested.
- Write timing excludes database creation and data generation. IPC includes transport, decoding, validation, persistence, and response. Disk size was measured after close and excludes peak temporary use.
- Results measure local macOS storage and give no direct estimate of Linux/GPU simulation throughput or model inference speed.

## 65536 metrics over a real Unix socket

| Mode | Elapsed | Relative throughput | Request P95 |
|---|---:|---:|---:|
| Per-record JSONL | 3.448 s | 1.00× | 129.0 ms |
| Batched JSONL | 1.207 s | 2.86× | 46.8 ms |
| Batched SQLite WAL FULL | 1.442 s | 2.39× | 64.1 ms |
| Batched SQLite WAL NORMAL | 1.533 s | 2.25× | 55.1 ms |

Batched JSONL was about 16% faster than SQLite FULL on this IPC workload. The modest FULL/NORMAL ordering difference does not establish a general ranking. Choose based on durability requirements.

### Writes and history queries

Direct medians for 64 records per batch:

| Mode | Write | Historical records for one episode | Disk after close |
|---|---:|---:|---:|
| Per-record JSONL | 1788.9 ms | 128.14 ms | 23.83 MiB |
| Batched JSONL | 292.5 ms | 128.61 ms | 23.83 MiB |
| SQLite FULL | 732.9 ms | 1.49 ms | 27.09 MiB |
| SQLite NORMAL | 674.0 ms | 1.51 ms | 27.09 MiB |

The historical query compared a complete JSONL scan to indexed SQLite filtering. Portal metrics normally uses memory aggregation/incremental loads, so this ratio does not estimate its speedup. SQLite metrics occupied slightly more space due to indices and pages.

## Trace storage without the duplicate JSONL writer

With 65536 records and 64/batch, dual write took about 4246 ms and SQLite-only about 4224 ms, a roughly 0.5% difference within noise. Total size fell from 143.00 MiB to 79.64 MiB (about 44.3%). The 8192-record trial shortened write time by about 15.5%, indicating dependence on volume and index overhead. Queries were essentially unchanged because both conditions queried SQLite.

## Recommended sequence for all benchmarks

1. Batch the common metrics append after validation and serialization. Preserve JSONL compatibility with Portal and exporters. Savings should come from fewer file operations and coarser locking.
2. Make trace JSONL an optional export and retain SQLite as the primary query store. Define replay, episode deletion, rotation, and export behavior. Expected benefits are less storage and duplicate serialization.
3. Add an optional SQLite metrics backend if historical filtering requires it. Provide a centralized writer, batched transactions, WAL, indices, and migrations. Update Portal queries, restart aggregation recovery, and export together.
4. Retain process stdout/stderr for startup diagnostics and keep large image/video objects outside the logging database.

No full simulation A/B was performed. A 2–3× logging throughput change cannot be converted into a 2–3× episode gain. With summary traces enabled, actual impact depends on logging's share of total time.

## Reproduction and raw results

```bash
PYTHONPATH=. python scripts/benchmark_log_storage.py \
  --records 65536 --repeats 3 \
  --output docs/changes/evidence/common-performance/log-storage-local-large.json
```

- [Benchmark script](../../scripts/benchmark_log_storage.py)
- [8192-record results](evidence/common-performance/log-storage-local.json)
- [65536-record results](evidence/common-performance/log-storage-local-large.json)
