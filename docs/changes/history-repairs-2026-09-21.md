# History catalog and batch execution repairs

[**English**](history-repairs-2026-09-21.md) · [简体中文](history-repairs-2026-09-21.zh-CN.md)

Date: 2026-09-21. These changes address five findings from the previous audit. Four later findings were deferred: action ledger exception recording, Planner output validation, GPT retries after cancellation, and stale Trace responses replacing newer ones. The four corresponding source files retain their initial SHA-256 hashes.

## Repairs

| Issue | Behavior after repair | Implementation |
| --- | --- | --- |
| Repeated cleanup restarts at the beginning of a large history directory | SQLite persists directory positions and candidate files. Scans rotate across experiments and resume after approximately 30 seconds when incomplete. Preview leaves the live cursor intact. Each round rechecks run locks, rerun markers, live processes, and path boundaries, then reports free disk space and the low-water mark. | [retention.py](../../robotenon/runtime/retention.py) |
| Episode index missing, corrupt, or unfinished after abnormal exit | Access queues background recovery from per-task JSON, rebuilding index, status, and summary counts without a full summary. Recovery holds the run lock and reports bad source files. Repairing those files permits retry. | [result_index.py](../../robotenon/task_adapters/result_index.py), [experiments.py](../../robotenon/web/experiments.py) |
| Large history increases startup, pagination, and state-save costs | jobs.sqlite3 stores the catalog. Update only changed rows. The cache is bounded to 128 entries and 8 MiB encoded. Scheduling loads active experiments and waiting jobs. Lists support indexed queries and cursor pagination. | [jobs.py](../../robotenon/storage/jobs.py) |
| Large batch results remain in memory and disk writes block execution | Save full results per task, keeping compact statistics in memory. A single background writer reuses SQLite connections, with at most 16 operations in flight. Cancellation waits for accepted writes. Abnormal exit leaves the index incomplete. | [result_writer.py](../../robotenon/task_adapters/result_writer.py), [evaluator.py](../../robotenon/task_adapters/evaluator.py) |
| Concurrent HTTP queries consume threads needed for controls | Default limits are 32 connections and 8 ordinary requests, with 4 additional slots for health and stop. Saturation returns 503 and Retry-After. | [http_server.py](../../robotenon/web/http_server.py), [server.py](../../robotenon/web/server.py) |

Recovery validates every source record before replacing the index. The replacement remains incomplete until status and summary are also saved. Interrupted recovery is retried on the next access. Long waiting queues are paginated so cache eviction cannot discard changes to a newly dispatched experiment.

## Verification

- Full Python suite: **526 passed, 1 skipped**, exit code 0. One existing warning concerned an unreachable Graph default branch.
- New fault and scale tests: **20 passed**, covering resumable/fair scans, slow prefix replay, preview, lock protection, single-row persistence, cursor stability, long queues, index corruption and retry, cancellation during initial writes, writer shutdown barriers, memory release, and HTTP capacity.
- Existing Node frontend suite: **20 passed**. Frontend scripts were unchanged.
- New and rewritten modules passed default Ruff checks. Existing large modules passed E9/F63/F7/F82 checks.
- Maintenance CLI verified index recovery, cleanup preview, and actual cleanup on temporary data without touching existing user experiments.

See [verification.json](history-repairs-2026-09-21/verification.json), [pytest.log](history-repairs-2026-09-21/pytest.log), the [Node log](history-repairs-2026-09-21/node-tests.log), and [CLI results](history-repairs-2026-09-21/cli-checks.json).

## Synthetic measurements

Tests used local macOS ARM64, Python 3.11.10, temporary directories, and fake tasks/drivers. Baseline was the complete workspace snapshot at the start of this round, including previous uncommitted changes. Each of three pairs ran baseline then repaired code. Values are medians over those three trials.

| Sample and metric | Before | After |
| --- | ---: | ---: |
| 20,000 history entries, normal restart initialization | 175.88 ms | 1.69 ms |
| Same sample, Python allocations at startup | 64.78 MB | 0.015 MB |
| Same sample, read 25 catalog records | 46.38 ms | 3.11 ms |
| Same sample, save one status change | 862.47 ms | 1.45 ms |
| 100 tasks, 256 KiB/result, concurrency 4, peak Python allocation | 27.18 MB | 2.06 MB |
| Same large-result batch, elapsed time | 0.520 s | 0.561 s |
| 300 fake tasks, concurrency 8, throughput | 388.14/s | 588.15/s |
| Fake action p95 latency | 1.367 ms | 1.469 ms |
| Fake action p99 latency | 1.531 ms | 2.598 ms |

For large results, peak Python allocation fell by about 92% and elapsed time rose by about 8%. Thread handoff, write backpressure, and waiting for completion on cancellation have a cost. Fake action tail latency rose in this round and varied across samples: post-repair p99 ranged from 1.54–5.02 ms. The action implementation was unchanged, and these values cannot establish a change in hardware latency.

Each catalog sample had 2 KiB of parameters. The list test substituted runtime-state refresh to measure metadata querying and serialization alone. Startup timing excluded first-time legacy JSON import, which still requires parsing old files. tracemalloc reports Python allocations, excluding RSS and some SQLite native allocations. It was enabled for the large-result memory trial but disabled for ordinary throughput, so elapsed times across those trials are not directly comparable.

[comparison.json](history-repairs-2026-09-21/comparison.json) contains full samples and extrema. Earlier unpaired trials in [preliminary](history-repairs-2026-09-21/preliminary/comparison.json) are separate from the final results. No real model, GPU, rendering, hardware, or long-duration acceptance run was performed.

Reproduction entry point:

```bash
python scripts/benchmark_history.py --output /tmp/history-after.json
python scripts/benchmark_runtime.py --output /tmp/runtime-after.json
python -m pytest -q tests/test_history_reliability.py
```

The benchmark imports the version in the runtime working directory. Run the same script from the corresponding baseline snapshot to compare versions.

## Upgrade and operation

1. On the next Portal startup, jobs.json is imported into jobs.sqlite3 once. The legacy JSON remains a pre-migration backup and receives no further updates. Custom live-status scripts must query the API or SQLite. This design assumes a local filesystem on one host.
2. offset remains supported for existing list pages. The API additionally supports next_cursor. Search and count have query budgets and may still slow with larger histories.
3. Automatic cleanup remains disabled by default. This change adds cursors and disk reporting. The scan time budget is soft: filesystem calls and prefix replay can exceed it, while each resumed scan advances to new entries.
4. Automatic index recovery requires a terminal experiment, released resources, and acquisition of its run lock. The operation restores the index without resuming the experiment. Queries during recovery return 503. index_recovery records diagnostic details.
5. Memory still scales with case lists, compact statistics, and final summary entries. An individual oversized result still requires processing. The 8 MiB metadata cache permits one oversized entry on its own.
6. Reserved slots admit controls when ordinary request slots are full. At 32 total connections, new control connections also receive 503. CPU, filesystem, and internal locks are still shared.

See [execution and observability](../execution-and-observability.md#14-historical-directories-index-recovery-and-request-capacity) for configuration, APIs, and maintenance commands.

[changes.patch](history-repairs-2026-09-21/changes.patch) records differences in 13 code, test, and configuration files relative to the initial snapshot. [source-manifest.json](history-repairs-2026-09-21/source-manifest.json) records fingerprints and evidence for deferred items. Documentation changes covered this record, the change index, and runtime reference. Existing workspace edits were preserved. No commit, service restart, or deployment was performed.
