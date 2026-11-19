# maniskill2: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.10; ManiSkill2 0.5.3; Vulkan.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Install requirements, then download the object datasets used by the profile from the simulator environment:
```bash
mkdir -p "$ASSETS/maniskill2"
cd "$ASSETS/maniskill2"
"$SIM_PY" -m mani_skill2.utils.download_asset -y PickSingleYCB-v0
"$SIM_PY" -m mani_skill2.utils.download_asset -y PickSingleEGAD-v0
"$SIM_PY" -m mani_skill2.utils.download_asset -y PickClutterYCB-v0
cd "$ROBOTENON"
export MS2_ASSET_DIR="$ASSETS/maniskill2/data"
```
Check the downloader's printed destination and set `MS2_ASSET_DIR` to that actual data root if different. Primitive PickCube smoke tasks do not establish that YCB/EGAD/clutter assets are present. SAPIEN requires a working Vulkan ICD; `nvidia-smi` alone does not verify rendering. Container drivers must expose graphics capability. CPU rendering requires a compatible software Vulkan ICD, not EGL settings.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/maniskill2/configs/eval.json" "$ROBOTENON/local-profiles/maniskill2.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark maniskill2 --profile "$ROBOTENON/local-profiles/maniskill2.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/maniskill2.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/maniskill2-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
