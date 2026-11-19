# RoboTenon performance and usability verification (2026-09-20)

[**English**](performance-2026-09-20.md) · [简体中文](performance-2026-09-20.zh-CN.md)

This round implemented six optimizations on top of R01–R11. Configuration snapshots, run locks, PID identity checks, Worker fault isolation, and resource release verification were preserved. The work did not start robots or model services, deploy remotely, or clean existing user experiments.

## Implementation

- **Background stop.** HTTP stop immediately returns `202 + pending=true`. Termination, grace periods, descendant cleanup, and release checks run in the background without holding the global lock while waiting. Repeated requests reuse the current operation. Failure preserves `stop_failed` and blocks reuse. Portal restart resumes authorized cleanup. UI stages distinguish runner exit, child cleanup, forced cleanup, release, and intervention.
- **Incremental results.** Per-task JSON remains authoritative and `episodes.sqlite3` indexes queries. A task transition updates only that task, counts, and current status. Initialization and controls write compact summaries. Finalization generates full `summary.json` and per-task-directory `stats.json`. Ordinary batches now write the cumulative summary once at initialization and once on completion. The scheduler refills available slots immediately while retaining a 100 ms maximum interval for checking controls.
- **Bounded queries.** Lists refresh only the requested experiment page. Episodes use SQLite. Trace limits time, rows, and bytes, returning diagnostic errors or continuation positions. Experiment, episode, and Trace pages support pagination/time ranges. SQL has next-page support. Batch controls validate indexed identities with at most 500 targets. Whole-experiment controls retain separate endpoints. The UI distinguishes current-page task counts from total successes.
- **Resource growth.** JSON LRU holds up to 256 entries and an estimated 32 MiB. A single JSON read is capped at 16 MiB. Configuration cache holds 64 entries and observation path mapping 4096. Video locks use weak references. Cache final process-release verification to avoid repeated process-table scans in history lists and invalidate it on rerun.
- **Startup diagnostics.** CLI, Portal submission, and startup checks share diagnostics for configuration, Graph outputs, GPU assignment, model paths, executables, disk, and ports. Optional GPU probes have a 2-second limit. TCP probes for explicitly addressed model IP endpoints have a 250 ms limit. Remote model identifiers, DNS endpoints, and GPUs unavailable to the host defer to runtime. Worker startup retains model-protocol readiness checks. The UI uses live snapshots to distinguish waiting, loading, running, cleanup, and intervention.
- **Worker notifications.** After measuring 1 / 4 / 8 idle Workers, replace both ends' 20 ms running polls with Pipe readability notifications. Health and heartbeat timers remain independent at 250 ms and 1 second. Cancellation removes read listeners. Fallback loops use a bounded threaded `poll`. Exit cleanup retains short polling and forced reaping.

## Measurements

Baseline was an isolated pre-optimization workspace snapshot including existing uncommitted changes, independent of Git HEAD. The main benchmark ran independently three times per version on local macOS/Apple Silicon with Python 3.11. Medians follow. See `performance-2026-09-20/comparison.json`, `before-1..3.json`, and `after-1..3.json` for samples and ranges.

Workload: 300 fake tasks with 8 slots and a 1 ms wait, 200 asynchronous fake-driver actions, 300 historical records and one experiment with simulated 500 ms cleanup, three rounds of 400 × 8 KiB JSON, and 20,000 trace samples. All data used temporary directories.

| Metric | Before | After |
| --- | ---: | ---: |
| Fake task throughput (tasks/s) | 76.71 | 407.10 |
| Elapsed for 300 tasks (s) | 3.911 | 0.737 |
| Total serialized JSON bytes written¹ | 131,290,902 | 4,164,236 |
| summary.json writes | 302 | 2 |
| Cumulative bytes written to summary.json | 68,821,830 | 251,185 |
| Stop request response (ms) | 513.131 | 1.374 |
| List read p95 during stop (ms)² | 545.368 | 0.739 |
| List response bytes (requested limit=25) | 79,318 | 7,282 |
| List records returned | 301 | 25 |
| Fake driver action p95 (ms) | 1.386 | 1.303 |
| Fake driver action p99 (ms) | 1.462 | 1.375 |
| Final bytes retained for 300 tasks³ | 810,269 | 924,589 |

¹ Counts JSON bytes written by `write_json` only, excluding SQLite/WAL, filesystem journals, and physical fsync writes. Throughput combines incremental persistence and scheduler changes and cannot be extrapolated to GPU inference or physical robots.

² Each round issued 8 sequential reads. Nearest-rank p95 includes the request blocked on the former global lock. Stop acknowledgement indicates acceptance while the approximately 500 ms cleanup continues. A separate regression verifies `released=false` before release.

³ The new index is about 311,296 bytes. Final storage increased about 14.1% while cumulative writes declined.

Median time for half the queue to finish fell from 1.924 to 0.404 seconds. Fake driver latency stayed in the same range. This check screens for regressions without establishing faster action execution.

Cache test: before optimization, entries grew to 400 / 800 / 1200, with about 10.86 MB tracked by tracemalloc in round three. Afterward it stayed at 256 entries and about 2.35 MB. With retention disabled, temporary JSON files grew from 3.28 MB to 9.85 MB. This tests capacity without a 24-hour or week-long soak.

The Trace first response changed from 20,000 rows/5,668,890 bytes to 1,000 rows/281,890 bytes with `next_offset`. The different payloads preclude a full-query throughput comparison.

Real idle Workers ran without models. Each group was sampled for 5 seconds. CPU is the combined Worker percentage of one core.

| Workers | CPU before | CPU after |
| --- | ---: | ---: |
| 1 | 1.36% | 0.21% |
| 4 | 4.64% | 0.71% |
| 8 | 9.20% | 1.43% |

RSS with 8 Workers was about 399.8 → 400.5 MiB. With a synthetic Pipe sender, the actual parent `receive` path improved from 18.067 → 0.095 ms at p95 and 18.162 → 0.116 ms at p99. This measures notification latency, excluding model inference and end-to-end actions.

## Interfaces and operation

| Interface | Pagination / time range | Default limits |
| --- | --- | --- |
| `GET /api/experiments` | `limit, offset, since, until, search` | Default 100, maximum 500 records, offset at most 100000, history refresh budget 1 second |
| `GET /api/experiments/<id>/episodes` | `limit, offset, since, until` | Default 100, maximum 500 records, filter by record update time |
| `GET /api/traces`, `GET /api/experiments/<id>/traces` | As above, also filter by episode/attempt | 500 ms and 3 MiB result budget |
| `POST /api/trace/sql` | `query, params, limit, offset, job_id` | Default 1000, maximum 10000 rows, 500 ms, 3 MiB. Specify `ORDER BY` in SQL. |
| `POST /api/preflight` | `config, overrides` | Read-only diagnostics without creating an experiment |

Use timezone-aware ISO 8601 parameters and narrow the time range when reaching the offset limit. Pagination returns `next_offset` and Trace also returns `truncated`. Total HTTP JSON limit is 4 MiB. Oversized results return 413, timeouts 504, and unknown experiments 404. Python `TraceQuery.sql()` raises on budget exhaustion so partial results cannot masquerade as complete. Use `sql_page()` for pagination.

```bash
# Read-only preflight. An error produces a nonzero exit code.
Tenon run --preflight --config configs/robotenon_xr365.json

# Preview retention without deleting files.
python scripts/maintain_experiments.py --output-root data/experiments \
  --policy configs/operations/retention-policy.json

# Explicitly apply retention to large files in terminal experiments.
python scripts/maintain_experiments.py --output-root data/experiments \
  --policy configs/operations/retention-policy.json --apply

# Apply the policy hourly while the Portal is running.
Tenon web --retention-policy configs/operations/retention-policy.json

# Rebuild the index for an existing experiment whose termination can be verified.
python scripts/maintain_experiments.py --index-run data/experiments/EXPERIMENT_ID
```

Default retention is 30 days for ordinary logs and traces and 7 days for video. Failed experiments retain these files for at least 90 days. Configuration, per-task results, summaries, and run locks are retained. Cleanup checks terminal state, exclusive run lock, pending reruns, live processes, and path containment without following symlinks. It skips legacy experiments lacking a verifiable lock. Each scan has time and entry budgets. Rerun maintenance when `truncated` is reported. **Automatic cleanup is disabled by default and this round deleted no user history.**

Small legacy summaries remain readable. Legacy JSON above 16 MiB yields a migration message. Use index rebuild for verifiably terminal historical experiments. `stats.json` now covers its task directory. Cross-task totals remain in `summary.json`. Preflight is diagnostic. Submission still performs execution and readiness checks. A deferred item is not a passed readiness check.

## Verification and limits

- Full Python suite: **506 passed, 1 skipped, 1 warning**. The skip required Linux native crash testing. The existing warning concerned an unreachable default route.
- Frontend Node: **20 passed**. JavaScript syntax, Python compilation, relevant static-error checks, and lint on new files passed.
- Added 26 regressions covering concurrent stops, cleanup failure, restart recovery, readable live indices, in-place rerun isolation, pagination/time validation, Trace timeout/size limits, LRU, retention lock/symlink protection, read-only preflight, Pipe cancellation/EOF, and loading phase.
- Isolated local HTTP/browser validation: 125 experiments paginated and searched correctly. 230 episodes split into 100 / 100 / 30 across pages, with next disabled on the last page. Navigation worked in both directions. Startup checks created no experiment, Trace SQL ran, and the browser console reported no errors.
- No real GPU/robot tests or long-duration stress run. `jobs.json` metadata still loads as a whole. Retention preserves result metadata and is independent of a total disk quota. Validate actual business throughput, model readiness after failure, and 24-hour memory/disk growth on target systems.

Reproduction commands (from the directory of the version being measured):

```bash
python scripts/benchmark_runtime.py --output /tmp/runtime-benchmark.json
python scripts/benchmark_workers.py --idle-seconds 5 --output /tmp/worker-benchmark.json
python -m pytest -o addopts='' -q
node --test tests/test_graph_routes.js tests/test_trajectory_media.js
```

[Results directory](performance-2026-09-20). `verification.json` lists changed files and hashes, `baseline-manifest.json` identifies pre-optimization files, and `performance-only.patch` contains only code changes relative to the snapshot after correctness repairs. Test logs, browser checks, raw samples, and comparisons were retained.
