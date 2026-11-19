# calvin: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.10; PyBullet/EGL; Linux.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Clone the simulator with its submodules; keep the Git metadata because CALVIN queries it at runtime.
```bash
git clone --recursive https://github.com/mees/calvin.git "$ASSETS/calvin"
git -C "$ASSETS/calvin" checkout fa03f01f19c65920e18cf37398a9ce859274af76
git -C "$ASSETS/calvin" submodule update --init --recursive
"$SIM_PY" -m pip install wheel 'setuptools==57.5.0'
"$SIM_PY" -m pip install --no-build-isolation -e "$ASSETS/calvin/calvin_env" -e "$ASSETS/calvin/calvin_models"
```
System build dependencies include CMake, C/C++, OpenBLAS and FFTW. Legacy PyHash/build failures must be resolved before running workers; installing the extension alone does not install these libraries.

Download the validation configuration from the pinned harness (no training trajectories required by this adapter):
```bash
git clone https://github.com/allenai/vla-evaluation-harness.git "$ASSETS/harness-reference"
git -C "$ASSETS/harness-reference" checkout 6cc3e1bd9fc4bed26fb0a3aabd342c5fed11ca09
```
Set `params.dataset_path` to `$ASSETS/harness-reference/docker/calvin_validation_data`. It must contain the saved `.hydra` configuration, including hidden files. For the full dataset use [CALVIN dataset instructions](https://github.com/mees/calvin/blob/fa03f01f19c65920e18cf37398a9ce859274af76/dataset/README.md); point at its `validation` directory, not the parent archive directory. Evaluation uses chained subtasks: report sequence completion as well as success, not an unrelated single-task score.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/calvin/configs/eval.json" "$ROBOTENON/local-profiles/calvin.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark calvin --profile "$ROBOTENON/local-profiles/calvin.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/calvin.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/calvin-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
