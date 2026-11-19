# Independent benchmark extension migration

[**English**](benchmark-migration.md) · [简体中文](benchmark-migration.zh-CN.md)

The benchmark integrations have been moved from duplicated checked-in Graph
configurations to one extension-owned protocol and one generated Graph path.

## Current contract

For each catalog entry:

- `vla.py` is the only model integration interface;
- `robotenon_benchmark_support.evaluate` is the evaluation command;
- `benchmark.py` owns simulator construction and protocol semantics;
- `_internal/` contains private migrated helpers;
- `configs/*.json` contains the upstream-derived profiles;
- `benchmark-source.json`, `LICENSE.upstream`, `NOTICE.upstream`, and
  `protocol-reference/` preserve source and license provenance;
- no runtime code imports `vla-evaluation-harness`.

The old `native/` package, `native_test*.json`, `native_eval.json`, root Portal
mirrors, and benchmark-specific checked-in Graph/Module manifests have been
removed. Old LIBERO policy/Graph adapters, the XR365 planner/verifier path, and
the RoboDojo RPC Graph path were also removed from these benchmark extensions.

## User workflow

```bash
# 1. Implement an independent model backend.
$EDITOR /path/to/my_model/vla.py

# 2. Run a selected upstream profile.
Tenon evaluate --benchmark libero \
  --backend /path/to/my_model/vla.py --model-python /path/to/model/python \
  --sim-python /path/to/libero/python \
  --profile extensions/benchmarks/libero/configs/spatial.json \
  --output runs/libero
```

RoboCasa365 additionally supports `--profile xiaomi_official --backend ./extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/vla.py --backend-config ./extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/config.json` with model replicas and
shared dynamic batching. Its speed tool is separate from accuracy evaluation.

## Validation gates

Repository tests enforce the public package layout, catalog/profile consistency,
profile-to-Graph generation, isolated imports, lifecycle order, observation dtype
transport, action validation, immediate terminal handling, per-task and
per-dimension aggregation, worker recycling, and failure accounting.

Real simulator smoke and differential evidence is recorded under `docs/changes/`.
Those reports establish the scope stated in each report; a synthetic action smoke
does not by itself prove full official score parity. Isaac simulator validation
requires a supported RTX host and licensed assets.

## Special cases

- Kinetix `realtime.json` is preserved as provenance and rejected by the sync
  evaluator until a dedicated live Graph exists.
- BEHAVIOR-1K and RoboDojo use different Isaac Sim stacks and should remain in
  separate environments.
- RoboCasa365 Xiaomi accuracy uses its official target50 protocol; throughput
  measurements use the separate speed tool.
