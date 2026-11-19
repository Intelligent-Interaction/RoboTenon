# robomme: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.11; pinned ManiSkill fork; Vulkan.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Install both pinned repositories through requirements.txt; upstream ManiSkill from PyPI is not a replacement for the RoboMME fork. Task definitions and evaluation metadata are supplied by [robomme_benchmark](https://github.com/RoboMME/robomme_benchmark/tree/f2b540e64034cf9afd0b51cf8ec491de09c74f58). First initialization can populate simulator asset caches; preserve them and verify a selected task before disconnecting the server.

The [robomme_data_h5 dataset](https://huggingface.co/datasets/Yinpei/robomme_data_h5) is for demonstration/training workflows; do not download it indiscriminately just to run the counting smoke. Imitation/reference task families may require their reference episode resources: initialize a task in each selected family before assuming all four dimensions are ready. Missing resources must be resolved, not counted as policy failures.

`counting`, `imitation`, `permanence` and `reference` select different task dimensions. Use the requested evaluation split and native action space (`joint_angle` in the provided counting profile). Do not infer full-suite coverage from the existing BinFill simulation probe. SAPIEN requires Vulkan even when inference is on another GPU.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robomme/configs/counting.json" "$ROBOTENON/local-profiles/robomme.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robomme --profile "$ROBOTENON/local-profiles/robomme.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robomme.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robomme-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
