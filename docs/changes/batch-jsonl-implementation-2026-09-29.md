# Batched JSONL writes for shared metrics (2026-09-29)

[**English**](batch-jsonl-implementation-2026-09-29.md) · [简体中文](batch-jsonl-implementation-2026-09-29.zh-CN.md)

## Change

MetricsDaemon.ingest_batch in `robotenon/infra/metrics.py` now acquires one lock, opens one file, appends one batch, and flushes once per batch. Single-record ingest uses the same path. All benchmarks using the shared metrics service inherit the change without extension configuration updates.

Format, validation, histogram boundaries, count aggregation, and incremental JSONL reads remain compatible. If the batch contains an invalid record or its input generator raises, the valid prefix is persisted in finally before the caller receives the error. File identity and offset are recorded after writing to avoid replaying just-written records on the first query.

Durability remains flush-only. There is no added fsync or timer-based buffering. Trace/episode storage, stdout/stderr, and model-batch metric snapshots were outside this change. A log write is not a database transaction and a disk or process failure during a write can leave a partial batch. The shared service owns each file. Concurrent direct writes by independent processes to the same JSONL file are outside this contract.

## Regression checks

All 55 checks passed, including new batch-log tests and existing metrics/trace, Portal, observability runtime, routing, and trace summary tests.

Coverage included Unicode, incremental queries, reload, repeated queries without double counting, invalid labels, conflicting histogram boundaries, valid-prefix persistence after iterator errors, concurrency/aggregation, read-only limits, and file replacement detection by readers.

## Production implementation measurements

Local macOS-27.0-arm64-arm-64bit, Python 3.11.10. Medians over 3 runs with 65536 synthetic records using the production metrics schema, 128 logical producers, at most 32 active Unix socket connections, and 64 records per request. The baseline retained the previous per-record file writer and used identical validation and in-memory aggregation. Prototype measurements are reported separately.

| Metric | Per-record JSONL baseline | Production batch JSONL |
|---|---:|---:|
| Total IPC time | 3.422 s | 1.144 s |
| IPC request P95 | 132.4 ms | 41.0 ms |

The logging path reached **2.99×** baseline throughput, reducing elapsed time by **66.6%**. Direct writes with 64 records per batch fell from roughly 1754 to 275 ms. All trials checked persisted record integrity and aggregates.

For single-record batches, direct time rose from about 1781 to 1955 ms (9.8%), mainly for batch management and file identity tracking. Benefits depend on callers supplying multiple records per submission. No artificial wait accumulated batches. These measurements do not imply a 3× simulation or GPU speedup. Server deployment and an end-to-end simulation A/B were still outstanding.

## Reproduction

```bash
PYTHONPATH=. python scripts/benchmark_log_storage.py --production \
  --records 65536 --repeats 3 \
  --output docs/changes/evidence/common-performance/production-batch-jsonl.json
```

[Raw results](evidence/common-performance/production-batch-jsonl.json) · [Storage comparison](log-storage-benchmark-2026-09-29.md)
