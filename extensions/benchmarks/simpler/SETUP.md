# simpler: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Linux Python 3.10; SAPIEN/Vulkan; NumPy 1.24.4.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Install the pinned ManiSkill2_real2sim and SimplerEnv revisions in requirements.txt. They supply robot/task definitions and packaged assets. Keep simulator caches/source data accessible to workers; initialize every selected task family before an offline run. See [pinned SimplerEnv setup](https://github.com/simpler-env/SimplerEnv/tree/4ab7178e83e84ee06894034ec6dbf9e7aad1e882) for additional scene/overlay assets used by visual variants. Training datasets such as Open X-Embodiment are not needed for online simulation.

Keep a checkout for the non-package `data/` directory (a wheel alone may omit it):
```bash
git clone https://github.com/simpler-env/ManiSkill2_real2sim.git "$ASSETS/ManiSkill2_real2sim"
git -C "$ASSETS/ManiSkill2_real2sim" checkout ef7a4d4fdf4b69f2c2154db5b15b9ac8dfe10682
"$SIM_PY" -m pip install -e "$ASSETS/ManiSkill2_real2sim"
export MS2_REAL2SIM_ASSET_DIR="$ASSETS/ManiSkill2_real2sim/data"
```
Verify `data/custom`, `data/hab2_bench_assets` and `data/real_inpainting` exist. The pinned [asset resolver](https://github.com/simpler-env/ManiSkill2_real2sim/blob/ef7a4d4fdf4b69f2c2154db5b15b9ac8dfe10682/mani_skill2_real2sim/__init__.py) reads `MS2_REAL2SIM_ASSET_DIR` before fallback paths.

Requirements use OpenCV 4.11.0.86: OpenCV 4.12 requires NumPy 2 on this Python stack and conflicts with the pinned NumPy 1.24.4. Do not upgrade it independently.

Visual matching (`*_vm-*`) and variant aggregation (`*_va-*`) are different protocols. Select all intended shards for a full comparison; one `google_robot_drawer_va-0` smoke is only a subset. Vulkan graphics support must work even on a headless inference machine. A CPU Lavapipe setup needs a compatible Vulkan implementation and does not validate NVIDIA rendering performance.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/simpler/configs/google_robot_drawer_va-0.json" "$ROBOTENON/local-profiles/simpler.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark simpler --profile "$ROBOTENON/local-profiles/simpler.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/simpler.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/simpler-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
