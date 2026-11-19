# robocerebra: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.10; MuJoCo 3.2.3; isolated LIBERO fork.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Use a separate environment **and separate `HOME`/LIBERO config** for each LIBERO fork; they all import the same `libero` package.
```bash
git clone https://github.com/qiuboxiang/RoboCerebra.git "$ASSETS/robocerebra"
git -C "$ASSETS/robocerebra" checkout 2573426c13dfcd5e7d7831c15587b058aaa1c0c0
export LIBERO_SOURCE="$ASSETS/robocerebra/LIBERO"
"$SIM_PY" -m pip install -e "$LIBERO_SOURCE"
mkdir -p "$HOME/.libero"
cat > "$HOME/.libero/config.yaml" <<EOF
benchmark_root: $LIBERO_SOURCE/libero/libero
bddl_files: $LIBERO_SOURCE/libero/libero/bddl_files
init_states: $LIBERO_SOURCE/libero/libero/init_files
datasets: $LIBERO_SOURCE/libero/datasets
assets: $LIBERO_SOURCE/libero/libero/assets
EOF
export MUJOCO_GL=egl
export NUMBA_DISABLE_JIT=1
```
The config is read in the simulator process. Keep the source checkout: editable installs and XML assets refer to it. Evaluation needs BDDL, meshes/textures and initial states; training demonstration HDF5 files are not required by the online evaluator.

Download the evaluation cases and initial states separately:
```bash
"$SIM_PY" -m pip install huggingface_hub
"$SIM_PY" -c 'import os; from huggingface_hub import snapshot_download; snapshot_download("qiukingballball/RoboCerebraBench", repo_type="dataset", local_dir=os.environ["ASSETS"]+"/RoboCerebra_Bench")'
```
Set `params.robocerebra_root` to the downloaded directory containing the case/task hierarchy. `task_types` selects dimensions such as `Ideal`; an Ideal-only run is not the complete benchmark.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robocerebra/configs/eval.json" "$ROBOTENON/local-profiles/robocerebra.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocerebra --profile "$ROBOTENON/local-profiles/robocerebra.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robocerebra.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robocerebra-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
