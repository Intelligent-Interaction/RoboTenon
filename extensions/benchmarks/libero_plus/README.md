# LIBERO-Plus

[**English**](README.md) · [简体中文](README.zh-CN.md)

[Benchmark catalog](../README.md) · [Installation and assets](SETUP.md) · [Quickstart](../QUICKSTART.md) · [Parallel execution and CLI reference](../EVALUATION.md)

The evaluation CLI uses the benchmark ID **`libero_plus`**. This extension defines the environment, tasks, seeds, horizon, observation and action specifications, and success criteria. Supply the model separately using `--backend`.

## Evaluation workflow

1. Follow the [setup guide](SETUP.md) to install the pinned simulator version, download assets, and configure paths.
2. Run the fake-action command in the setup guide. Verify that the environment advances, images render correctly, and no runtime errors occur.
3. Connect a model using the [model interface](../models/README.md) and validate it on a small task subset.
4. Select a profile below. Follow the [evaluation reference](../EVALUATION.md) for single- or multi-GPU execution.

## Selecting an evaluation protocol

The default starting profile is `all-0`. In each JSON file, `params` configures the environment and `episodes_per_task` sets the number of episodes per task. For entries marked “unspecified,” consult the benchmark implementation for the default.

| Profile | Task set or mode | Episodes per task |
| --- | --- | ---: |
| [`all-0`](configs/all-0.json) | libero_spatial | 1 |
| [`all-1`](configs/all-1.json) | libero_object | 1 |
| [`all-2`](configs/all-2.json) | libero_goal | 1 |
| [`all-3`](configs/all-3.json) | libero_10 | 1 |
| [`spatial`](configs/spatial.json) | libero_spatial | 1 |

`all-0` through `all-3` select spatial, object, goal, and libero_10, respectively. `spatial` aliases `all-0`; include it only once. Use `--profiles all-0 all-1 all-2 all-3` to generate a combined plan, shuffle and schedule it globally, and aggregate by episode count. For separate runs, retain the suite dimension and weight results by actual episode counts. Select perturbation subsets with `category` and `difficulty_level` in external JSON `params`.

`--profile` accepts the names above or an external JSON file. Copy a profile before changing asset paths, then use absolute paths. For a small validation run, set `--max-tasks` and `--episodes-per-task`. Record overrides and the actual subset in published results.

## Minimal command

After installation, run this command from the RoboTenon repository root. See the [quickstart](../QUICKSTART.md) for `MODEL_PY` and `SIM_PY`.

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_plus --profile all-0 \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/libero_plus-model-smoke
```

## Results and verification

Read the `summary` field in `runs/evaluation/status.json` during execution and `summary.json` when the run completes. Results include per-episode and per-task statistics and the dimensions defined by the benchmark. `protocol.json` records the configuration and scheduling settings. `episode-plan.json` lists the actual episodes. See [outputs and progress](../EVALUATION.md#10-outputs-and-progress).

Xiaomi Robotics 0 achieved **74.77%** across all four suites and 10,030 cases on eight H100 GPUs
with an end-to-end duration of **2 h 7 min 57 s**. For configuration, results by suite, perturbation category and difficulty, and batch distributions, see
[the full Xiaomi Robotics 0 LIBERO-Plus evaluation](../../../docs/tutorials/benchmark-h100-xiaomi-libero-plus.md).

For validation on real simulators and hardware requirements, see the [setup guide](SETUP.md#verification-scope). Source revisions and licenses are documented in [benchmark-source.json](benchmark-source.json), [requirements.txt](requirements.txt), `LICENSE.upstream`, and `NOTICE.upstream`.
