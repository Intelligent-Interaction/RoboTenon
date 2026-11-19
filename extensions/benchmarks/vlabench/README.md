# VLABench

[**English**](README.md) · [简体中文](README.zh-CN.md)

[Benchmark catalog](../README.md) · [Installation and assets](SETUP.md) · [Quickstart](../QUICKSTART.md) · [Parallel execution and CLI reference](../EVALUATION.md)

The evaluation CLI uses the benchmark ID **`vlabench`**. This extension defines the environment, tasks, seeds, horizon, observation and action specifications, and success criteria. Supply the model separately using `--backend`.

## Evaluation workflow

1. Follow the [setup guide](SETUP.md) to install the pinned simulator version, download assets, and configure paths.
2. Run the fake-action command in the setup guide. Verify that the environment advances, images render correctly, and no runtime errors occur.
3. Connect a model using the [model interface](../models/README.md) and validate it on a small task subset.
4. Select a profile below. Follow the [evaluation reference](../EVALUATION.md) for single- or multi-GPU execution.

## Selecting an evaluation protocol

Xiaomi-Robotics-1 completed the full evaluation on eight H100 GPUs in approximately **1 h 54 min**, with **60.84%** task-macro success. See the [Xiaomi VLABench report](../../../docs/tutorials/benchmark-h100-xiaomi-vlabench.md) for the configuration, all 50 task results, and throughput.

The default starting profile is `eval`. In each JSON file, `params` configures the environment and `episodes_per_task` sets the number of episodes per task. For entries marked “unspecified,” consult the benchmark implementation for the default.

| Profile | Task set or mode | Episodes per task |
| --- | --- | ---: |
| [`eval`](configs/eval.json) | ['select_fruit', 'select_toy', 'select_drink'] | 3 |
| [`full`](configs/full.json) | Tracks 1, 2, 3, 4, and 6, totaling 50 entries | Up to 50 each, 2,460 overall |

The `full` profile reads tracks 1, 2, 3, 4, and 6 from `$VLABENCH_ROOT/configs/evaluation/tracks`. Its 50 task-track entries use up to 50 configurations each. In the recorded asset revision, track 2 `insert_flower` has only ten configurations, yielding 2,460 cases.

`--profile` accepts the names above or an external JSON file. Copy a profile before changing asset paths, then use absolute paths. For a small validation run, set `--max-tasks` and `--episodes-per-task`. Record overrides and the actual subset in published results.

## Asset and model downloads

The links below come from the pinned [official downloader](https://github.com/OpenMOSS/VLABench/blob/cf588fe60c0c7282174fe979f5913170cfe69017/scripts/download_assets.py). Download on the simulator host and follow the [setup guide](SETUP.md).

| Asset | Source | Requirement and destination |
| --- | --- | --- |
| VLABench source, robot/base assets, track definitions | [OpenMOSS/VLABench](https://github.com/OpenMOSS/VLABench), revision `cf588fe60c0c7282174fe979f5913170cfe69017` | Required. Track files are in the package's `configs/evaluation/tracks/`. |
| Object meshes and textures | [Google Drive: obj.zip](https://drive.google.com/file/d/1ldEMZua2OzXHJTYTCP0IGVU1aFYBCMu-/view) | Required. Extract into `$VLABENCH_ROOT/assets` to populate `obj/`. |
| Scene assets | [Google Drive: scene.zip](https://drive.google.com/file/d/1KdReRkibJClBHHD32jz_wTkaBzhEJ9Kw/view) | Required. Extract into `$VLABENCH_ROOT/assets` to populate `scenes/`. |
| Xiaomi VLABench checkpoint and processor | [Hugging Face: Xiaomi-Robotics-1-VLABench](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-VLABench) | Required for Xiaomi. Use the VLABench-specific checkpoint. |

Set `VLABENCH_ROOT` to the **Python package directory** containing `assets` and `configs`. For a checkout in `/data/assets/VLABench`, this is `/data/assets/VLABench/VLABench`.

```bash
export ASSETS=/data/assets
git clone https://github.com/OpenMOSS/VLABench.git "$ASSETS/VLABench"
git -C "$ASSETS/VLABench" checkout cf588fe60c0c7282174fe979f5913170cfe69017
export VLABENCH_ROOT="$ASSETS/VLABench/VLABench"
mkdir -p "$VLABENCH_ROOT/assets"
# Follow SETUP first to install VLABench and dependencies such as gdown.
"$SIM_PY" "$ASSETS/VLABench/scripts/download_assets.py" --choice all

hf download XiaomiRobotics/Xiaomi-Robotics-1-VLABench \
  --local-dir /data/models/Xiaomi-Robotics-1-VLABench
```

The official script also accepts `--choice asset` and `--choice scene` to retry individual packages. To download in parallel, replace `--choice all` above with:

```bash
"$SIM_PY" "$ASSETS/VLABench/scripts/download_assets.py" --choice asset &
asset_pid=$!
"$SIM_PY" "$ASSETS/VLABench/scripts/download_assets.py" --choice scene &
scene_pid=$!
wait "$asset_pid"; asset_status=$?
wait "$scene_pid"; scene_status=$?
test "$asset_status" -eq 0 && test "$scene_status" -eq 0
```

If Google Drive quota or network errors interrupt the download, fetch both ZIP archives using the links above. Verify archive integrity and extract to the same destination. Check for HTML error pages saved as ZIPs and avoid nesting another assets directory. Verify `assets/base`, `assets/robots`, `assets/obj`, `assets/scenes`, and the textures referenced by track 6. Run the [fake-action installation check](SETUP.md#smoke-with-fake-actions) and confirm reset for the intended track. Inference evaluation requires no training demonstrations.

## Minimal command

After installation, run this command from the RoboTenon repository root. See the [quickstart](../QUICKSTART.md) for `MODEL_PY` and `SIM_PY`.

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark vlabench --profile eval \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/vlabench-model-smoke
```

## Results and verification

Read the `summary` field in `runs/evaluation/status.json` during execution and `summary.json` when the run completes. Results include per-episode and per-task statistics and the dimensions defined by the benchmark. `protocol.json` records the configuration and scheduling settings. `episode-plan.json` lists the actual episodes. See [outputs and progress](../EVALUATION.md#10-outputs-and-progress).

For validation on real simulators and hardware requirements, see the [setup guide](SETUP.md#verification-scope). Source revisions and licenses are documented in [benchmark-source.json](benchmark-source.json), [requirements.txt](requirements.txt), `LICENSE.upstream`, and `NOTICE.upstream`.
