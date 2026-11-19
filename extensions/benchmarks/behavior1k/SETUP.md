# behavior1k: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Linux RTX GPU; Isaac Sim 4.5; OmniGibson 3.7.2; Python 3.10.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Follow the [BEHAVIOR v3.7.2 installation](https://github.com/StanfordVL/BEHAVIOR-1K/tree/v3.7.2) for Isaac/OmniGibson, then install extension requirements in that simulator environment. This is not the same Isaac stack as RoboDojo.

Required assets: OmniGibson robot assets, BEHAVIOR scene/object dataset and **2025 challenge task instances**. Review [dataset terms](https://behavior.stanford.edu/dataset) and NVIDIA Isaac Sim EULA. The extension's first initialization requests acceptance and invokes `download_omnigibson_robot_assets`, `download_behavior_1k_assets` and `download_2025_challenge_task_instances` from `omnigibson.utils.asset_utils`. Run the first initialization interactively with a persistent writable data volume before scheduling unattended workers.

The destination is `omnigibson.macros.gm.DATA_PATH`. Verify it contains `2025-challenge-task-instances` as well as the robot/object/scene bundles; a marker directory alone does not establish a complete download. Preserve this directory across container runs. Training demonstrations are not needed by this online adapter.

Only after accepting the relevant NVIDIA terms, set the required EULA environment variables in the simulator environment. For headless runs use `OMNIGIBSON_HEADLESS=1`. Start with the provided single-task profile and one worker. This integration remains **unvalidated on real Isaac rendering** because the available A100 machine does not provide the required RTX rendering platform.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/behavior1k/configs/eval.json" "$ROBOTENON/local-profiles/behavior1k.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark behavior1k --profile "$ROBOTENON/local-profiles/behavior1k.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/behavior1k.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/behavior1k-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

Interface/configuration tests only; real simulation, frames and success signals are not yet certified on RTX hardware.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
