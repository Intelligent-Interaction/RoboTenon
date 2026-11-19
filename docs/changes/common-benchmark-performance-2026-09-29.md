# Common benchmark performance optimization — 2026-09-29

[**English**](common-benchmark-performance-2026-09-29.md) · [简体中文](common-benchmark-performance-2026-09-29.zh-CN.md)

## Scope

All changes are in core shared batching/transport or benchmark-support. No benchmark-specific simulation adapter or model backend was changed. The provisional Xiaomi preprocessing edits were removed at the user's request. Worker counts, GPU routing, episode ordering, batching wait policy, model RNG, observations and horizons are unchanged. Nothing was deployed to the active server evaluation.

## Changes

- MessagePack + raw NumPy bytes for local batch RPC, replacing JSON + Base64 on new writes. Numerical dtypes and shapes are preserved; object arrays remain rejected. Size limits remain. New readers accept legacy JSON; old readers do not accept new messages, so model and simulator processes must upgrade together. Framework installs now require msgpack.
- Shared sync/live history borrows read-only views of packed snapshots rather than copying them a second time. The initial simulator-to-snapshot copy remains. Public decode keeps its writable-copy default. Array lifetime follows its NumPy base, and future simulator steps cannot overwrite the owned snapshot.
- Shared policy reuses validated/normalized action data instead of converting it to JSON-compatible values twice.
- Every shared model replica publishes small atomic aggregate metrics at startup, every five seconds and shutdown: batch histogram, requests, inference/queue time, pending depth/peak and session count. No growing per-request history was added.

## Local CPU measurements

Baseline commit: `014dad454912447ec7bd41055280f05006735ea6`.
Payload: 4 history observations ×3 cameras ×256×256 uint8 RGB plus float32 state. Synthetic, model-independent data. 30 timed repetitions after warmup on local macOS ARM64, Python 3.11.10, NumPy 2.4.6.

| Operation | Before median | After median | Ratio |
| --- | ---: | ---: | ---: |
| Encode/decode roundtrip | 12.420 ms | 1.134 ms | 10.96× |
| Snapshot decode for history | 0.00973 ms | 0.00296 ms | 3.29× |
| Actual Unix socket full-payload echo | 34.819 ms | 9.818 ms | 3.55× |

Message bytes: 3,147,240 → 2,360,359 (about 25% smaller). Exact reconstructed images/states checked. Socket echo returns the full observation as well as sending it, whereas real inference returns a much smaller action; this is a transport stress test, not an episode latency prediction. The decode optimization saves microseconds in this fixture and is not claimed as a large overall speedup.

Reproduce from the repository root in an environment with framework and benchmark-support installed:

```bash
python extensions/benchmarks/benchmark-support/tools/benchmark_transport.py --output /tmp/common-transport.json --repeats 30
```

[Raw measurement](evidence/common-performance/local-transport.json).

## Correctness coverage

The common tests cover all 20 benchmark fake-action configurations through the new wire format; read-only history snapshot ownership; non-contiguous, endian-specific, bool, integer and floating arrays; legacy messages; invalid payloads and limits. Process tests verify live metric snapshots, single/batch execution, session isolation and shutdown. Existing native Graph, unified backend, realtime, execution and entry-audit regression tests are included.

Result: **140 passed in 25.98 seconds** across `test_benchmark_performance_paths`, `test_dynamic_batching`, `test_native_graph_configs`, `test_unified_benchmark_backend`, `test_live_benchmark`, `test_benchmark_entry_audit`, `test_live_status_window` and `test_execution_backends`.

This does not claim a fresh real-simulator run of all 20 benchmarks, GPU action parity or full-evaluation throughput acceleration. No worker-count tuning or task-affinity/cross-GPU scheduler change was applied. Those require stable workload measurements and separate protocol/RNG review, rather than assuming upstream scheduling is universally faster.

## Server validation of c18ab58c

The previous evaluation was stopped at 153 completed episodes, 124 successes and zero errors. Its results and a `manual-stop.json` were retained; all 140 owned processes exited on SIGTERM. Both model and simulation processes then used the new source tree. MessagePack was installed directly on the server in the simulator environment; the model environment already provided it.

- The same 140 regression tests passed on the server.
- Real Xiaomi smoke run: 8 GPUs, 16 simulation workers per GPU, dynamic batch limit 8, collection timeout 50 ms. All **128/128 episodes completed without errors**, using 128 distinct workers and 128 unique episode IDs. Each of eight tasks had 16 episodes, and every episode executed 32 environment steps. This intentionally capped horizon is a runtime test, not an official accuracy result.
- All eight model services reported closed, with no pending requests or sessions. All eight GPUs returned to 0 MiB used.
- Actual batch histogram: 246 batches of size 1 and five batches of size 2 (256 requests total). Staggered initialization and short episodes did not exercise full batches of eight; this test does not establish saturated batch throughput.
- A separate real OpenDrawer rollout verified that all three 256×256 cameras changed after actions; transported images were pixel-identical and borrowed history views were read-only. Terminal and boolean success fields were returned normally.
- Server CPU transport microbenchmark: encode/decode 14.50 → 0.83 ms; Unix socket echo 33.72 → 7.26 ms (4.65×). These are communication measurements, not end-to-end evaluation speedup.

Run directory: `/path/to/deployment/xiaomi-128worker-smoke-20260929-143300`. The smoke test ended normally; a new target50 × 50 evaluation was not started as part of this validation.

Evidence: [runtime checks](evidence/common-performance/server-validation.json), [image hashes](evidence/common-performance/server-render-check.json), [transport timings](evidence/common-performance/server-transport.json).
