# Shared trace summaries and observation history cache

[**English**](trace-history-implementation-2026-09-29.md) · [简体中文](trace-history-implementation-2026-09-29.zh-CN.md)

## Scope

These changes apply to all benchmarks using the unified entry point. They use common code and preserve the Side Chat system Graph, planner/verifier Tools, subgoal, and action chunk state interfaces. No deployment or restart of the active server evaluation occurred.

- Shared `TraceClient` and Graph/Tool calls: summary mode omits serialization of detailed inputs, outputs, and observation references for successful calls and records bounded aggregates. Exceptions and Tool failures retain separate error records. Final episode output and live status are preserved. Full mode retains detailed traces.
- Shared `BatchClient`/service connections: content hashes for history frames, missing-frame transport, and per-connection LRU. Both sides cache serialized data. A hit reconstructs independent arrays so model-side mutation does not affect cached data.
- `SharedBatchModel` and synchronous/realtime benchmark runtimes share option forwarding and failed-client cleanup. `vla.py` requires no changes.
- Unified evaluate adds `trace_mode=summary` and `history_cache=auto` and reports the effective settings. Model and simulator environments remain isolated. Scheduling, task seed, horizon, RNG, and success behavior are preserved.

## Memory and recovery

Each side of a connection keeps its own cache of up to 32 frames or 64 MiB. Oversized history falls back to ordinary messages. Disconnect destroys server cache. On error, the client clears its cache and removes the failed connection from the Tool. A new connection starts a new session and sends all frames in its first request without transparently retrying inference. Upgrade model services and Workers together.

Caching does not compress frames. Forcing it on disjoint histories adds hashing and serialization overhead. Auto mode uses backend declarations to select potential beneficiaries. Actual savings still require measurement.

## Read-only server memory diagnosis

The server was running `c18ab58c` with the new cache disabled. Over two samples approximately 4.2 minutes apart, total RSS for 128 simulator Workers rose from roughly 616.3 GiB to 624.5 GiB. The later sample had about 314 GiB available and 287 GiB of filesystem cache. Model/launcher RSS totaled about 17.5 GiB. Summed RSS includes shared pages. A spot check of the largest Worker found about 6 GiB, mostly private anonymous memory.

Filesystem caching alone did not explain the growth, and the undeployed history cache could not have caused it. With `recycle_after_tasks=1000` and about 20 tasks per Worker, task-count recycling rarely triggered. The samples do not establish a native simulator leak. Inspect episode-boundary cleanup and consider memory-watermark Worker recycling, which was outside this implementation.

## Verification

Cache tests cover exact pixels/state, repeated frames, LRU bounds, invalid messages, fallback, isolation from in-place changes, multiple sessions, batches 1/2, reconnect, and timeout cleanup. Summary tests cover bounded counts, retained errors, and consistent Graph execution/live terminal status. Regression coverage includes Side Chat multi-model systems and synchronous/realtime Graphs.

The production RPC microbenchmark used real BatchClient, BatchEngine, Unix sockets, and shared codecs. The model returned state IDs and performed no GPU inference. Inputs had three 256×256 images, history=4, interval=2. Modes alternated for 140 measured samples each after five warm-up requests. See [RPC measurements](evidence/common-performance/production-history-cache.json).

See [Graph measurements](evidence/common-performance/production-trace-summary.json) for the real Graph comparison. Benchmark tools are at `extensions/benchmarks/benchmark-support/tools/benchmark_history_cache.py` and `benchmark_optimization_candidates.py`. These local shared-path results do not estimate whole-evaluation or cross-simulator speedups.

## Final local results

- 197 regression tests passed in 37.66 seconds, including the unified entry point, realtime paths, multi-model systems, shared configurations for 20 benchmarks, and performance paths.
- Isolated rerun of a 400-node Graph: full 891.57 ms → summary 726.01 ms, an 18.6% reduction in Graph time.
- Real RPC microbenchmark at replan=2: 4.73 ms with cache disabled, 2.45 ms enabled (1.93×).
- Real RPC microbenchmark at replan=16: 5.04 ms disabled, 6.63 ms enabled (0.76×).
- Forced caching was slower at replan=16. Default auto leaves it disabled for the current Xiaomi configuration.
- `git diff --check` passed. No commit, push, or deployment to the active server occurred.
