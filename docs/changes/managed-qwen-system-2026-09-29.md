# Managed deployment of the hierarchical Xiaomi + Qwen system

[**English**](managed-qwen-system-2026-09-29.md) · [简体中文](managed-qwen-system-2026-09-29.zh-CN.md)

## Integration

Previously, systems contained only fake services and a template requiring a separately started Qwen API. The running Xiaomi-only default Graph also lacked hierarchy. A Qwen3-VL Provider, managed batch service configuration, and system Module/Tool/Graph configurations now allow the unified evaluator to start and stop all model services.

- Qwen model code lives in `extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen`, outside the benchmark.
- Planner and verifier Tools explicitly pass purpose and share one or more `reasoner` replicas. Requests retain request_id/session routing.
- Native images go directly to the Qwen processor. The model process performs JSON schema validation and retries truncated output.
- Deployment supports Xiaomi/Qwen on the same GPU or on separate GPUs. Workers prefer a local GPU service and preserve separate model/simulator Python environments.
- Smoke runs CloseFridge and OpenDrawer once each for up to 32 steps. Full runs retain official target50×50 seeds/horizons.
- Smoke enables full trace and full evaluation uses summaries. Environment success remains authoritative alongside the visual verifier.

## Initial verification

- All 20 local tests passed: existing hierarchical system and Qwen HTTP Tool regressions, new Provider image/JSON/retry/session checks, and real-process Graph runs for one sample and batch=2. The generator was a substitute without real weights.
- Server model-runtime used torch 2.6.0+cu124 and transformers 4.57.1. The real processor from `/data/models/Qwen3-VL-4B-Instruct` generated input_ids, pixel_values, and image_grid_thw from images on CPU.
- Fixed content list handling required by Transformers 4.57.1.
- Prepared smoke deployment, backend, and execution JSON in the separate server directory `/path/to/deployment` without loading GPU models.

## Real GPU verification

After the user stopped the full VLA-only run, authorization covered immediate verification followed by the full hierarchical run. The earlier waiting script was retired.

- real-smoke: 2/2 completed, 0 errors/cancellations, 32 environment steps each. Qwen planner ran 4 times and verifier 2 times, with 0 JSON retries and 3 camera views per call. Session count reached 0 after shutdown.
- batch-validation: 16/16 completed, 0 errors/cancellations or duplicate episodes, 32 steps each. Xiaomi handled 32 requests in 17 batches and Qwen 48 in 19. Both reached actual batch size 6 with a configured maximum of 8 and 50 ms wait.
- Local Provider and common batching suites passed 31 tests. After same-GPU routing changes, 5 Provider checks passed including a new test with two services on one GPU.
- None of the 32-step smoke runs reached environment success. They validate integration, without establishing success-rate or accuracy parity.
- The requested deployment used 8 GPUs, one resident Xiaomi and Qwen per GPU, 16 simulator Workers per GPU, batch size 8, and 50 ms wait. Run colocated-validation for capacity and lifecycle before full-target50x50.
- Concurrent validation enabled summary traces. Xiaomi's auto history cache disabled itself for the current window and replan interval.

Server evidence: `/path/to/deployment/validation-evidence.json`.

## Final directory layout

Xiaomi/Qwen implementations moved to the system tool/ directory and Modules to module/. Graphs remain in configs/graphs.json. No graph/ directory is needed. The system-specific run.py was removed. Unified evaluate loads system.qwen_batch.json and execution.xiaomi_qwen.json. Model implementation behavior was preserved and protocol/system tests were rerun.

## Colocation validation and full-run startup

- colocated-validation: Xiaomi/Qwen on the same GPU with 16 Workers. All 16/16 completed with 0 errors. Largest actual batches were Xiaomi 5 and Qwen 6. Sessions and services closed normally.
- forced-batch8-capacity: a test adapter padded each forward to 8 samples with 16 simulator Workers. All 16/16 finished with 0 errors and every Xiaomi forward recorded batch 8. The adapter is excluded from production and the run was not an accuracy comparison.
- The colocation test reached 40192 MiB, close to 40 GB capacity. Longer text or different scenes might exceed this peak. The full run requires monitoring.
- The new server layout passed 15 regressions. Locally 15 system tests passed, plus 24 protocol/backend tests with 1 skipped because torch was absent.
- Example full configuration: 8 GPUs each hosting Xiaomi + Qwen, 128 Workers, batch 8 with 50 ms wait, and a JSON Graph loaded by unified evaluate. Official task seeds/horizons apply. Results belong to the hierarchical system and should be reported separately from Xiaomi-only results.
