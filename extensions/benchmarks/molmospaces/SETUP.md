# molmospaces: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.12 recommended for the pinned resource stack; MuJoCo.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

The simulator and resource package are separate. Install the pinned simulator requirements in a Python version accepted by its package metadata; the previous server validation used Python 3.12. Do not assume the old Python 3.11 recipe works with every resource-package resolution.
```bash
export MLSPACES_ASSETS_DIR="$ASSETS/molmospaces"
export MLSPACES_CACHE_DIR="$ASSETS/molmospaces-cache"
export MUJOCO_GL=egl
export JAX_PLATFORMS=cpu
mkdir -p "$MLSPACES_ASSETS_DIR" "$MLSPACES_CACHE_DIR"
"$SIM_PY" -c 'import molmo_spaces; from molmo_spaces.evaluation.benchmark_schema import load_all_episodes; print("asset initialization complete")'
find "$MLSPACES_ASSETS_DIR/benchmarks" -type d -name '*json_benchmark'
```
Import triggers the upstream resource downloader/extractor. It retrieves scenes, objects, robots and benchmark descriptions from [allenai/molmospaces](https://huggingface.co/datasets/allenai/molmospaces). Retain both cache and extracted assets; training demonstrations are not required.

Set `params.benchmark_dir` to the exact extracted benchmark directory (the profile defaults to the 20260212 200-episode Franka PickAndPlace set). Do not substitute another dated benchmark silently. If the installed resource manifest does not contain that set, install the matching resource version or provide `params.resource_manifest` mapping resource types/sources to published versions. A missing benchmark directory is a setup error, not zero success. Record resource versions alongside results; simulator version alone does not freeze assets.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/molmospaces/configs/pick_and_place.json" "$ROBOTENON/local-profiles/molmospaces.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark molmospaces --profile "$ROBOTENON/local-profiles/molmospaces.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/molmospaces.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/molmospaces-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
