# Unified benchmark and model migration — 2026-09-29

[**English**](unified-benchmark-entry-2026-09-29.md) · [简体中文](unified-benchmark-entry-2026-09-29.zh-CN.md)

## Layout and command

All benchmark extensions, benchmark-support and model backends now live below
`extensions/benchmarks/`. The public native evaluation command is
`Tenon evaluate`. A backend is a standalone file path
or `module:factory`; no central registration is needed. Legacy extension commands
forward to the shared parser; callers must provide `--backend`. Old VLA factories
remain for existing generated configs.

Model parameters are passed unchanged from backend JSON to the factory. Protocol
profiles remain benchmark-owned. Device counts, workers, batch limits and scheduling
are execution options. A model can use any supported benchmark profile, including
smoke or a subset; expected counts come from enumerated episodes, not a hardcoded
Xiaomi denominator. Reports distinguish builtin/external/overridden profiles and
record the complete effective profile and backend declaration.

## Scheduling

The default is an independently seeded shuffle (seed 7), then the core global
pending queue and idle process-slot queue. Original case identity, native task
index, episode index and environment parameters are not rewritten by shuffle.
GPU/model endpoint/EGL bindings are fixed to worker slots and interleaved across
GPUs at startup. Core attempt records include Worker/GPU identity and timing;
`episode-plan.json` records initial queue order and `protocol.json` records policy.
Retries reuse the episode identity and final statistics count the outcome once.

Optional `--scheduling-strategy static` assigns `case.index % worker_count` to a
fixed slot. It deliberately permits load imbalance. `--no-shuffle` is independent
of scheduling strategy. The only core code change replaces three literal
`backend == "process"` configuration restrictions with the existing
`managed_workers` capability check, allowing the extension's static process backend
to reuse normal resource, retry and worker lifecycle code.

## Validation evidence

Local full regression: **903 passed, 2 skipped** (local torch unavailable and a
Linux-only worker fault check). The server model protocol suite passed all **16**
checks, including the whole-model BF16 buffer regression.

- External file backends support relative imports and distinct module namespaces.
- Subprocess tests ensure the simulator never imports a model-only module.
- Batch 1 and batch 2 use the same Graph, including reverse-order response routing,
  non-Xiaomi history/replan declarations, immediate termination and session cleanup.
- A deliberately killed worker is retried, with six unique final episode records
  and no error or duplicate-count inflation.
- Independent shuffle preserves all case data and global Python RNG state.
- Real server model comparison uses identical observations/noise at batch 1 and 8:
  migration action max absolute difference is **0.0** for both. See
  [raw parity result](evidence/unified-benchmark/model-migration-parity.json).
- Both fake and real Xiaomi completed **four** RoboCasa365 smoke episodes (two tasks,
  two episodes each, 24-step cap), with **zero errors/cancellations**. Worker CUDA/EGL
  binding used GPUs 0 and 1. Model interpreter was `model-runtime` (Python 3.12);
  simulator was the separate RoboCasa environment (Python 3.11). No injected model
  site-packages were used. GPU memory returned to zero after service shutdown.
- The fake run formed actual batches of size 2. The short Xiaomi run's requests
  did not coalesce within 50 ms (all forwards had size 1); separate real-model
  batch-8 input parity above validates the migrated batched implementation.

These 24-step smoke episodes had zero task successes and are not an accuracy test.
The earlier repaired CloseFridge 47/50 result predates this structural migration.
No new full target50 × 50 accuracy run is claimed here. Other simulator protocols
are covered by their existing contract regressions, not newly rerun full physics
or Isaac evaluations. EGL finalizer protection and explicit whole-model BF16
conversion are preserved.

## Server artifacts

Under `/path/to/deployment/validation-xiaomi-20260929/`:

- `model-migration-parity.json` and `.log`
- `unified-migration/fake/` and `unified-migration/xiaomi/`
- `model-env-install.log` records the direct server installation of Transformers
  4.57.1 in model-runtime, replacing its incompatible version.
