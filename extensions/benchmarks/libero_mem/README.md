# LIBERO-Mem

[**English**](README.md) · [简体中文](README.zh-CN.md)

[Benchmark catalog](../README.md) · [Installation and assets](SETUP.md) · [Quickstart](../QUICKSTART.md) · [Parallel execution and CLI reference](../EVALUATION.md)

The evaluation CLI uses the benchmark ID **`libero_mem`**. This extension defines the environment, tasks, seeds, horizon, observation and action specifications, and success criteria. Supply the model separately using `--backend`.

## Evaluation workflow

1. Follow the [setup guide](SETUP.md) to install the pinned simulator version, download assets, and configure paths.
2. Run the fake-action command in the setup guide. Verify that the environment advances, images render correctly, and no runtime errors occur.
3. Connect a model using the [model interface](../models/README.md) and validate it on a small task subset.
4. Select a profile below. Follow the [evaluation reference](../EVALUATION.md) for single- or multi-GPU execution.

## Selecting an evaluation protocol

The default starting profile is `eval`. In each JSON file, `params` configures the environment and `episodes_per_task` sets the number of episodes per task. For entries marked “unspecified,” consult the benchmark implementation for the default.

| Profile | Task set or mode | Episodes per task |
| --- | --- | ---: |
| [`eval`](configs/eval.json) | libero_mem | 50 |

`--profile` accepts the names above or an external JSON file. Copy a profile before changing asset paths, then use absolute paths. For a small validation run, set `--max-tasks` and `--episodes-per-task`. Record overrides and the actual subset in published results.

## Minimal command

After installation, run this command from the RoboTenon repository root. See the [quickstart](../QUICKSTART.md) for `MODEL_PY` and `SIM_PY`.

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_mem --profile eval \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/libero_mem-model-smoke
```

## Results and verification

Read the `summary` field in `runs/evaluation/status.json` during execution and `summary.json` when the run completes. Results include per-episode and per-task statistics and the dimensions defined by the benchmark. `protocol.json` records the configuration and scheduling settings. `episode-plan.json` lists the actual episodes. See [outputs and progress](../EVALUATION.md#10-outputs-and-progress).

For validation on real simulators and hardware requirements, see the [setup guide](SETUP.md#verification-scope). Source revisions and licenses are documented in [benchmark-source.json](benchmark-source.json), [requirements.txt](requirements.txt), `LICENSE.upstream`, and `NOTICE.upstream`.
