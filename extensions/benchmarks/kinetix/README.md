# Kinetix

[**English**](README.md) · [简体中文](README.zh-CN.md)

[Benchmark catalog](../README.md) · [Installation and assets](SETUP.md) · [Quickstart](../QUICKSTART.md) · [Parallel execution and CLI reference](../EVALUATION.md)

The evaluation CLI uses the benchmark ID **`kinetix`**. This extension defines the environment, tasks, seeds, horizon, observation and action specifications, and success criteria. Supply the model separately using `--backend`.

## Evaluation workflow

1. Follow the [setup guide](SETUP.md) to install the pinned simulator version, download assets, and configure paths.
2. Run the fake-action command in the setup guide. Verify that the environment advances, images render correctly, and no runtime errors occur.
3. Connect a model using the [model interface](../models/README.md) and validate it on a small task subset.
4. Select a profile below. Follow the [evaluation reference](../EVALUATION.md) for single- or multi-GPU execution.

## Selecting an evaluation protocol

The default starting profile is `eval`. In each JSON file, `params` configures the environment and `episodes_per_task` sets the number of episodes per task. For entries marked “unspecified,” consult the benchmark implementation for the default.

| Profile | Task set or mode | Episodes per task |
| --- | --- | ---: |
| [`eval`](configs/eval.json) | See profile | 100 |
| [`realtime`](configs/realtime.json) | live | 100 |

`--profile` accepts the names above or an external JSON file. Copy a profile before changing asset paths, then use absolute paths. For a small validation run, set `--max-tasks` and `--episodes-per-task`. Record overrides and the actual subset in published results.

## Minimal command

After installation, run this command from the RoboTenon repository root. See the [quickstart](../QUICKSTART.md) for `MODEL_PY` and `SIM_PY`.

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark kinetix --profile eval \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/kinetix-model-smoke
```

## Real-time evaluation

The same public command and model interface support `--profile realtime`:

```bash
Tenon evaluate \
  --benchmark kinetix --profile realtime \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --backend-config ./extensions/benchmarks/models/fake/configs/kinetix.json \
  --model-python /path/to/model/python --sim-python /path/to/kinetix/python \
  --workers-per-gpu 2 --batch-size 2 --wait-ms 5 --output runs/kinetix-live
```

The built-in profile expects RTC levels at `/app/rtc/worlds`. For another asset
location or a small smoke subset, copy the profile to an external JSON and change
`params.rtc_worlds_dir`, `params.tasks`, `params.max_episode_steps` and
`episodes_per_task`; pass that JSON through `--profile`. Use `render: "cpu"` for
JAX CPU execution.

Live Graph: `reset → policy → execute → observe → live_tick → terminal → policy`.
`policy` selects the newest delivered action without waiting for inference.
Before the first reply or on a stale tick, Kinetix supplies a zero/last-fresh hold.
`live_tick` queues the next observation and paces the clock. The model service
processes observation messages in FIFO order, serves cached chunk actions without
new inference, and returns responses asynchronously. Every observation contributes
to backend-declared history. Multiple responses between ticks collapse to the
latest action, matching the upstream non-CI action buffer.

Protocol options: `hz` (positive frequency), `pace` (positive speed multiplier),
`wait_first_action` (default false; true waits at most 30 seconds). Each tick waits
for its remaining period divided by pace; there are no catch-up bursts after a
slow physics step. A native terminal ends immediately without another observation
request. Already-submitted inference is drained for clean session closure and its
unused action is discarded. Model runtime setup is excluded from the live clock.

Episode `outputs.rt_metrics` contains `update_count`, `stale_count`,
`stale_action_ratio`, `effective_control_hz`, `step_time_mean`, `step_time_max` and
wall-clock `elapsed_sec`. Success/horizon/seed remain benchmark-owned. The stale
ratio uses the upstream denominator `updates + stale ticks`; it need not equal
stale ticks divided by environment steps if multiple replies arrive between ticks.

This implements the upstream default non-continuous-inference message behavior;
CI/LAAS model-specific strategies are not enabled implicitly. The target Hz is not
a hardware guarantee: JAX compilation and slow physics appear in the timing metrics.

## Results and verification

Read the `summary` field in `runs/evaluation/status.json` during execution and `summary.json` when the run completes. Results include per-episode and per-task statistics and the dimensions defined by the benchmark. `protocol.json` records the configuration and scheduling settings. `episode-plan.json` lists the actual episodes. See [outputs and progress](../EVALUATION.md#10-outputs-and-progress).

For validation on real simulators and hardware requirements, see the [setup guide](SETUP.md#verification-scope). Source revisions and licenses are documented in [benchmark-source.json](benchmark-source.json), [requirements.txt](requirements.txt), `LICENSE.upstream`, and `NOTICE.upstream`.
