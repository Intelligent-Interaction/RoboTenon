# robotwin: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Linux Python 3.10; CUDA toolchain; SAPIEN 3.0.0b1.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

This adapter currently expects the checkout at **`/app/RoboTwin`** (not a configurable environment variable). Provision or mount that path in the simulator environment:
```bash
git clone https://github.com/RoboTwin-Platform/RoboTwin.git /app/RoboTwin
git -C /app/RoboTwin checkout c3ddfa8b97d5519efa828b075999bd0006778e5e
cd /app/RoboTwin/assets
"$SIM_PY" _download.py
unzip objects.zip
unzip embodiments.zip
unzip background_texture.zip
cd /app/RoboTwin
"$SIM_PY" script/update_embodiment_config_path.py
cd "$ROBOTENON"
```
Use the pinned requirements (including CuRobo v0.7.8). Install PyTorch before building CuRobo with `--no-build-isolation`; `nvcc`, Ninja and a matching CUDA toolkit are needed for CUDA extensions. Assets include embodiments, objects and backgrounds; regenerate embodiment paths after moving the checkout. Check the upstream setup's SAPIEN/MPLib compatibility patches for this pinned release.

The provided profile disables expert solvability checks for an evaluation-only qpos path. Enabling oracle checks needs the complete planner installation. The existing real probe covers `grab_roller/demo_clean`, not all tasks/configurations. Training trajectories are not needed for this probe.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robotwin/configs/eval.json" "$ROBOTENON/local-profiles/robotwin.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robotwin --profile "$ROBOTENON/local-profiles/robotwin.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robotwin.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robotwin-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
