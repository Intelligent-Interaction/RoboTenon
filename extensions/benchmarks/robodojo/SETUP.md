# robodojo: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Linux RTX GPU; Isaac Sim 5.1; Python 3.11 official stack.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Use the pinned upstream Docker/build environment for Isaac and its binary dependencies; this extension's requirements alone are insufficient.
```bash
git clone --recursive https://github.com/RoboDojo-Benchmark/RoboDojo.git "$ASSETS/RoboDojo"
git -C "$ASSETS/RoboDojo" checkout ee67a1468510da7624a089164402359f2afc72c8
git -C "$ASSETS/RoboDojo" submodule update --init --recursive
cd "$ASSETS/RoboDojo"
bash scripts/init_assets.sh
cd "$ROBOTENON"
export ROBODOJO_ASSETS="$ASSETS/RoboDojo/Assets"
```
The script downloads [RoboDojo assets](https://huggingface.co/datasets/RoboDojo-Benchmark/RoboDojo), using Git LFS; install `git-lfs` first. Assets include scenes, robots and objects and require substantial disk capacity (upstream recipe estimates about 90 GiB). Review repository/dataset and NVIDIA terms before fetching/using them. Set profile `params.root` to the checkout visible inside the simulator environment/container. Mount assets at the matching path; a host path inaccessible inside the container does not work.

Follow the [pinned Dockerfile](https://github.com/RoboDojo-Benchmark/RoboDojo/blob/ee67a1468510da7624a089164402359f2afc72c8/Dockerfile) for Isaac installation. A100 is not an RTX rendering validation platform. Start with one task and one worker because Isaac application lifetime is process-global. This integration has **not** passed real Isaac simulation validation in the available environment; do not interpret interface tests as verified renders.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robodojo/configs/smoke_test.json" "$ROBOTENON/local-profiles/robodojo.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robodojo --profile "$ROBOTENON/local-profiles/robodojo.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robodojo.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robodojo-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

Interface/configuration tests only; real simulation, frames and success signals are not yet certified on RTX hardware.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
