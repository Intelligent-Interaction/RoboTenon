# RoboCasa365

[**English**](README.md) · [简体中文](README.zh-CN.md)

[Benchmark catalog](../README.md) · [Installation and assets](SETUP.md) · [Quickstart](../QUICKSTART.md) · [Parallel execution and CLI reference](../EVALUATION.md)

The evaluation CLI uses the benchmark ID **`robocasa365`**. This extension defines the environment, tasks, seeds, horizon, observation and action specifications, and success criteria. Supply the model separately using `--backend`.

## Evaluation workflow

1. Follow the [setup guide](SETUP.md) to install the pinned simulator version, download assets, and configure paths.
2. Run the fake-action command in the setup guide. Verify that the environment advances, images render correctly, and no runtime errors occur.
3. Connect a model using the [model interface](../models/README.md) and validate it on a small task subset.
4. Select a profile below. Follow the [evaluation reference](../EVALUATION.md) for single- or multi-GPU execution.

## Selecting an evaluation protocol

The default starting profile is `smoke`. In each JSON file, `params` configures the environment and `episodes_per_task` sets the number of episodes per task. For entries marked “unspecified,” consult the benchmark implementation for the default.

| Profile | Task set or mode | Episodes per task |
| --- | --- | ---: |
| [`smoke`](configs/smoke.json) | pretrain | 1 |
| [`xiaomi_official`](configs/xiaomi_official.json) | pretrain | 50 |

`xiaomi_official` selects target50 × 50, pretrain, seed 7, 256-pixel observations, and the native task horizons. `smoke` checks integration.

`--profile` accepts the names above or an external JSON file. Copy a profile before changing asset paths, then use absolute paths. For a small validation run, set `--max-tasks` and `--episodes-per-task`. Record overrides and the actual subset in published results.

## Minimal command

After installation, run this command from the RoboTenon repository root. See the [quickstart](../QUICKSTART.md) for `MODEL_PY` and `SIM_PY`.

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile smoke \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/robocasa365-model-smoke
```

## Xiaomi Robotics 1

Set `model_path` in `../models/xiaomi/config.json`, then run:

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./extensions/benchmarks/models/xiaomi/config.json \
  --model-python /envs/model/bin/python --sim-python /envs/robocasa/bin/python \
  --model-devices 0,1,2,3,4,5,6,7 --workers-per-gpu 16 \
  --batch-size 8 --wait-ms 50 --output runs/robocasa365-xiaomi
```

The formal profile selects target50 × 50, pretrain, base seed 7, stride 50,
256-pixel cameras and native task horizons. Xiaomi can also use `--profile smoke`;
other compatible models may use `xiaomi_official`. The model does not select the
protocol. Single inference uses `--batch-size 1 --workers-per-gpu 1 --wait-ms 0`.

The model preserves the verified `.cuda().to(torch.bfloat16)` cast, including RoPE
buffers. The simulator preserves the EGL finalizer context guard: old renderer
cleanup must bind its own context then restore the active one to avoid RGB corruption.
Dynamic batching can change floating-point results and global RNG assignment;
compare success rates, not bitwise trajectory identity. Throughput-only tools live
under `tools/` and do not produce official accuracy claims.


## Results and verification

Read the `summary` field in `runs/evaluation/status.json` during execution and `summary.json` when the run completes. Results include per-episode and per-task statistics and the dimensions defined by the benchmark. `protocol.json` records the configuration and scheduling settings. `episode-plan.json` lists the actual episodes. See [outputs and progress](../EVALUATION.md#10-outputs-and-progress).

For validation on real simulators and hardware requirements, see the [setup guide](SETUP.md#verification-scope). Source revisions and licenses are documented in [benchmark-source.json](benchmark-source.json), [requirements.txt](requirements.txt), `LICENSE.upstream`, and `NOTICE.upstream`.

## Evaluating a planner, VLA, and verifier

Use the separate [RoboCasa system package](../systems/robocasa_hierarchical/README.md) to reuse the legacy XR365 hierarchical graph. It uses this extension's current Environment, Body, TaskAdapter, and protocol. Model implementations and decision logic live under models/ and systems/, respectively. The VLA-only evaluation uses the same entry point as before.
