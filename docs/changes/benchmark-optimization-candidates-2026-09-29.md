# Local evaluation of shared benchmark optimizations

[**English**](benchmark-optimization-candidates-2026-09-29.md) · [简体中文](benchmark-optimization-candidates-2026-09-29.zh-CN.md)

## Findings

Prioritize an optional compact trace mode in the shared runtime. The current full Xiaomi evaluation does not warrant incremental history caching or cross-GPU session migration. Initialization concurrency and CPU thread settings need matched server workloads. This round added independent test tools and evidence without changing production paths or interrupting server jobs.

The measurements preserved ongoing work on the unified system interface. Tests ran on macOS ARM64 with 10 logical CPUs and Python 3.11.10, without GPUs, model weights, or simulation assets. Graph tests ran the real framework. Queue tests used discrete-event simulation.

## 1. Per-node traces and optional summary mode

The real `run_episode` ran 400 sequential nodes returning small results, using the `SharedObservability` Unix socket aggregation and persistence path. The three configurations enabled trace/live status, disabled trace only, or disabled both. After one warm-up per condition, five alternating trials verified that all 400 nodes ran with identical results. Timing included configuration loading, Graph execution, episode output, and observability shutdown. Service startup was excluded.

| Mode | Median elapsed time |
|---|---:|
| Trace and live status | 885.61 ms |
| Trace disabled, live status enabled | 691.21 ms |
| Trace and live status disabled | 689.99 ms |

Disabling trace cut **idle Graph time by approximately 22%**, amortized to roughly 0.49 ms/node. This is not simulator-stage timing. Disabling already-throttled live status saved a further 1.22 ms, below sample variation.

Consider a shared optional summary or sampled trace mode preserving final results, errors, counts, and lifecycle events. Disabling all traces gives an upper bound on savings. This test excludes real physics, rendering, inference, and contention from 128 processes writing shared server storage, so it cannot imply a 22% end-to-end gain.

## 2. Incremental history caching depends on the backend

Using the existing MessagePack codec, the prototype reconstructed a 4-frame history at interval 2 for three 256×256 RGB views plus state. It sent missing frame numbers and retained bounded receiver-side state. Images and state matched element by element. Five alternating trials measured cache maintenance and codec time, excluding the first request, sockets, GPU, and inference.

| replan_steps | Full history | Incremental prototype | Codec ratio | Mean bytes |
|---|---:|---:|---:|---:|
| 1 | 1.000 ms | 0.215 ms | 4.66× | 2.36 MB → 0.65 MB |
| 2 | 0.972 ms | 0.200 ms | 4.86× | 2.36 MB → 0.59 MB |
| 4 | 1.061 ms | 0.533 ms | 1.99× | 2.36 MB → 1.18 MB |
| 16 | 1.240 ms | 1.217 ms | 1.02× (within noise) | 2.36 MB → 2.36 MB |

Xiaomi declares `length=4, interval=2, replan_steps=16`, so adjacent model requests have no overlapping frames. The initial trial was slightly slower and a rerun slightly faster. **Transmitted bytes did not decrease.** Sending each new environment frame to the model service could increase traffic from 4 frames every 16 steps to all 16.

Backends with short replanning intervals could use an on-demand missing-frame protocol. The prototype still needs disconnect, retry, session cleanup, and cross-service routing tests before production use.

## 3. Cross-GPU global inference queue

The closed-loop discrete-event simulation used 128 clients, 8 services, a maximum batch of 8, a 50 ms wait, and 100 requests per client. Both schedulers replayed the same per-client delays. Median speedup was computed across three seeds, verifying that all 12,800 requests appeared exactly once.

The assumed service curve was 0.4 seconds for one sample plus 0.02, 0.1, or 0.4 seconds per additional sample. Simulated wait varied from 0.5–1.5 times the mean. These assumptions were not fit to GPU measurements.

| Mean simulated wait | Global queue speedup relative to fixed binding |
|---|---:|
| 2 seconds | 0.994–1.056× |
| 6 seconds | 0.997–1.013× |
| 12 seconds | 0.994–1.003× |

With a 6-second wait and 0.02 seconds per additional sample, mean batch size grew from about 1.37 to 1.96 while throughput improved by only about 1.3%. **A larger average batch alone does not establish higher end-to-end throughput.**

The simulation omitted session migration, global queue traffic, differing batch keys, heterogeneous GPUs, NUMA, RNG constraints, and severe GPU imbalance. It cannot rule out gains under other workloads. Measure service curves at batch 1/2/4/8 and record actual arrivals for replay before restructuring. Cross-service scheduling under `official_stream` changes RNG assignment.

## 4. Initialization and CPU concurrency

The real shared `startup_gate` ran with 32 warmed local processes, 64 tasks, and gate limits of 8/16/32. Each condition ran three trials using either 50 ms waits or SHA-256 CPU work. Process startup was excluded.

| Synthetic workload | Concurrency 8 | Concurrency 16 | Concurrency 32 |
|---|---:|---:|---:|
| Waiting | 0.454 s | 0.231 s | 0.112 s |
| CPU | 0.244 s | 0.220 s | 0.214 s |

Wait-bound work scaled nearly linearly. CPU work saved roughly 12% from 8→32, and less than 3% from 16→32. The experiment did not model asset loading, EGL, GPU contention, or numerical library threads and provides insufficient evidence for a server setting of 32.

Correction: `native_reset_parallelism` gates Worker bootstrap until the first successful reset and observation. It does not throttle every episode reset. Increasing the limit mainly improves cold startup and cannot resolve steady-state throughput in a running evaluation.

Worker count, BLAS/OpenMP thread count, and CPU affinity still need comparison under real simulation workloads. The CPU hash test did not validate these settings. Use matched tasks/seeds and real step/render timings for the next server A/B.

## Reproduction and evidence

Run from the repository root using Python with NumPy, msgpack, and the framework test dependencies.

```bash
PYTHONPATH=.:tests:extensions/benchmarks/benchmark-support/src \
python extensions/benchmarks/benchmark-support/tools/benchmark_optimization_candidates.py \
  --output /tmp/local-candidates.json

PYTHONPATH=.:extensions/benchmarks/benchmark-support/src \
python extensions/benchmarks/benchmark-support/tools/benchmark_worker_capacity.py \
  --output /tmp/local-worker-capacity.json
```

- [History, Graph, and queue evidence](evidence/common-performance/local-candidates.json)
- [Concurrency experiments](evidence/common-performance/local-worker-capacity.json)

These results screen candidates. They do not predict speedups across all benchmarks or guarantee an evaluation completion time.
