# Shared benchmark runtime

[**English**](README.md) · [简体中文](README.zh-CN.md)

[Benchmark catalog](../README.md) · [Quickstart](../QUICKSTART.md) · [Evaluation reference](../EVALUATION.md) · [Model interface](../models/README.md)

This directory contains the shared CLI and runtime adapters. Use the guides above to run evaluations. The following sections describe the implementation.

## Source layout

| File | Responsibility |
| --- | --- |
| `evaluate.py` | CLI parsing, protocol validation, model service and simulator process lifecycle |
| `backend_loader.py` | File/module backends, isolated namespaces, and SPEC validation |
| `runtime.py` | Synchronous history, action chunks, and session lifecycle |
| `live_runtime.py` | Real-time messaging and selection of the latest action |
| `configure.py` | Application, graph, and module configuration generation |
| `integration.py` | Environment, Body, TaskAdapter, and native step boundaries |
| `scheduling.py` | Shuffle a stable episode list with independent RNG |
| `static_execution.py` | Optional fixed assignment to worker slots |
| `execution.py` | native_sync and native worker-instance lifecycle |
| `codec.py` | Transport observation arrays and images |
| `dimensions.py`, `results.py`, `recording.py` | Metric dimensions, aggregation, and recording |

The unified command defaults to process workers and separate model services. Low-level `configure`, legacy policy adapters, and `native_sync` are developer interfaces. New models follow the batch interface in `models/README.md`, including B=1 for a single sample.

## Extension responsibilities

A benchmark defines tasks, seeds, horizon, success, and native observations/actions. A model owns checkpoints, preprocessing, RNG, and action conversion. The runtime owns history, chunks, scheduling, and process lifecycle. Configure synchronous and real-time behavior explicitly. Models should not change the simulation clock implicitly.

Keep new model backends separate from compatibility modules in `robotenon.benchmarks` and legacy extension-level `vla.py` files. Retain upstream provenance and licenses. Protocol alignment tests complement task-level checks of physics and success rates.

## Tests and probes

Run these commands in a test environment with the framework and relevant extension installed:

```bash
python -m pytest tests/test_native_graph_configs.py tests/test_unified_benchmark_backend.py tests/test_dynamic_batching.py tests/test_live_benchmark.py
```

The standalone probe `python -m robotenon_benchmark_support.smoke --help` supports scripted actions, `--save-frames`, and `--render gpu|cpu`. Inspect images, state, and changes across steps. A no-op action may legitimately produce a static frame. Software rendering still needs the relevant drivers.

## Validation records

- [Native action and image comparisons](../../../docs/changes/native-action-parity-2026-09-28.md) cover selected tasks in 18 non-Isaac environments. Full benchmark scores require separate evaluation.
- [Unified entry point and model migration](../../../docs/changes/unified-benchmark-entry-2026-09-29.md).
- [Kinetix realtime](../../../docs/changes/kinetix-live-unified-2026-09-29.md).
- [Bounded live status](../../../docs/changes/bounded-live-status-2026-09-29.md) retains the latest 256 records, refreshes once per second in ordinary execution, and writes errors/termination immediately.

These records document development history. Consult current user guides for installation, assets, and commands.

## Native system graphs

The unified evaluator accepts `--system-config` and `--execution-config` and reuses the framework configuration loader, SharedBatchServices, and tool lifecycle. system_config.py composes configurations while protecting the benchmark's environment and protocol. model_tool.py sends native observations to non-action model services. See [system integration](../SYSTEMS.md).
