# robocasa365: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.11; Linux MuJoCo/EGL.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

RoboCasa 0.2 and RoboCasa365 1.0.1 require separate environments and assets. Pin both RoboCasa and robosuite:
```bash
git clone https://github.com/robocasa/robocasa.git "$ASSETS/robocasa365"
git -C "$ASSETS/robocasa365" checkout b4684e6ee37d377cc392e98302a6b916d588b415
"$SIM_PY" -m pip install 'git+https://github.com/ARISE-Initiative/robosuite.git@5ce6643f3092639d08f7b0f90ed1c6a84f50552c'
"$SIM_PY" -m pip install -e "$ASSETS/robocasa365"
cd "$ASSETS/robocasa365"
"$SIM_PY" robocasa/scripts/setup_macros.py
"$SIM_PY" robocasa/scripts/download_kitchen_assets.py
cd "$ROBOTENON"
export MUJOCO_GL=egl
```
The asset downloader installs kitchen layouts, textures, objects and fixtures relative to the simulator's configured asset root. Follow its prompts and confirm all downloads finish. A two-task smoke asset subset is insufficient for a full suite. Demonstration datasets are not required for online rollout; model weights belong to the model environment.

Keep the extension's renderer lifecycle fix enabled. In multi-GPU jobs the unified runner binds simulator EGL and model devices; do not override worker-specific bindings globally. Inspect rendered images after reset as well as after actions.

For Xiaomi accuracy use `xiaomi_official` (target50 × 50, seed 7, per-task horizon), not the smoke/speed layout. See the [Xiaomi model guide](../models/xiaomi/README.md) for weights and dependencies. Record RNG mode and execution settings. The full command is in the README. A smoke score measures only the selected subset.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robocasa365/configs/smoke.json" "$ROBOTENON/local-profiles/robocasa365.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile "$ROBOTENON/local-profiles/robocasa365.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robocasa365.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robocasa365-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
