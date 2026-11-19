# libero_plus: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.10; MuJoCo 3.2.3; isolated LIBERO fork.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Use a separate environment **and separate `HOME`/LIBERO config** for each LIBERO fork; they all import the same `libero` package.
```bash
git clone https://github.com/sylvestf/LIBERO-plus.git "$ASSETS/libero_plus"
git -C "$ASSETS/libero_plus" checkout 4976dc30028e805ff8094b55501d532c48fec182
export LIBERO_SOURCE="$ASSETS/libero_plus"
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

Additional Plus meshes/textures are mandatory:
```bash
curl -fL --retry 5 'https://huggingface.co/datasets/Sylvest/LIBERO-plus/resolve/main/assets.zip?download=true' -o "$ASSETS/libero-plus-assets.zip"
mkdir -p "$ASSETS/libero-plus-extracted"
unzip "$ASSETS/libero-plus-assets.zip" -d "$ASSETS/libero-plus-extracted"
find "$ASSETS/libero-plus-extracted" -type d -name assets
```
The archive has nested directories. Copy the **contents** of the extracted `assets` directory into `$LIBERO_SOURCE/libero/libero/assets/`; do not create `assets/assets`. Install system ImageMagick for Wand-based visual variants. Download the complete assets archive before selecting variation profiles.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/libero_plus/configs/all-0.json" "$ROBOTENON/local-profiles/libero_plus.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_plus --profile "$ROBOTENON/local-profiles/libero_plus.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/libero_plus.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/libero_plus-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
