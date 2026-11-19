# rlbench: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.10; CoppeliaSim Edu 4.1.0; Linux X11/GLX.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Use the compatible simulator release, not current CoppeliaSim. Review [CoppeliaSim licensing](https://www.coppeliarobotics.com/) and [RLBench license](https://github.com/stepjam/RLBench) before using the Edu/academic stack.
```bash
curl -fL --retry 5 https://downloads.coppeliarobotics.com/V4_1_0/CoppeliaSim_Edu_V4_1_0_Ubuntu20_04.tar.xz -o "$ASSETS/coppeliasim.tar.xz"
mkdir -p "$ASSETS/coppeliasim"
tar -xJf "$ASSETS/coppeliasim.tar.xz" -C "$ASSETS/coppeliasim" --strip-components=1
export COPPELIASIM_ROOT="$ASSETS/coppeliasim"
export LD_LIBRARY_PATH="$COPPELIASIM_ROOT:${LD_LIBRARY_PATH:-}"
export QT_QPA_PLATFORM_PLUGIN_PATH="$COPPELIASIM_ROOT/platforms"
export QT_PLUGIN_PATH="$COPPELIASIM_ROOT"
export QT_QPA_PLATFORM=xcb
"$SIM_PY" -m pip install cffi
"$SIM_PY" -m pip install --no-build-isolation 'git+https://github.com/stepjam/PyRep.git@8f420be8064b1970aae18a9cfbc978dfb15747ef'
"$SIM_PY" -m pip install --no-deps 'git+https://github.com/stepjam/RLBench.git@02720bba4c73fe02eb75df946b8791b806028a9d'
```
Task scene/model assets ship with RLBench/PyRep and the simulator; offline demonstration datasets are not required. Install Xvfb and X11/GLX system libraries. On a headless machine prefix the evaluation command with `xvfb-run -a -s '-screen 0 1280x1024x24'`. Do not use Qt `offscreen`: it can initialize successfully but produce black frames.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/rlbench/configs/eval.json" "$ROBOTENON/local-profiles/rlbench.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark rlbench --profile "$ROBOTENON/local-profiles/rlbench.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/rlbench.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/rlbench-install-check"
```

Use the Xvfb prefix above. For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
