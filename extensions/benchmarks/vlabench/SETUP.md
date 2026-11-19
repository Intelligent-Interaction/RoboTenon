# vlabench: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.10; MuJoCo 3.2.2 / dm_control 1.0.22.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Download task meshes, textures and scene assets with the pinned source's downloader:
```bash
git clone https://github.com/OpenMOSS/VLABench.git "$ASSETS/VLABench"
git -C "$ASSETS/VLABench" checkout cf588fe60c0c7282174fe979f5913170cfe69017
"$SIM_PY" -m pip install -e "$ASSETS/VLABench"
export VLABENCH_ROOT="$ASSETS/VLABench/VLABench"
cd "$ASSETS/VLABench"
"$SIM_PY" scripts/download_assets.py
cd "$ROBOTENON"
export MUJOCO_GL=egl
```
The upstream downloader retrieves object and scene archives (Google Drive). Check exit status and extracted asset contents; an empty directory after a quota/network failure is not a successful install. The downloader must run on the machine storing simulator assets. Demonstration datasets/model checkpoints are optional for environment rollout.

The upstream default does not guarantee deterministic resets. The extension offers explicit seed adaptation; record whether it is enabled when comparing runs. See the native parity report for the seeded probe's scope.

## Configure local paths

### Reproduce the Xiaomi full-run runtime optimization

The measured run used lazy Open3D imports in eight upstream Python files. The exact changes are tracked in [patches/lazy-open3d.patch](patches/lazy-open3d.patch). Apply once to the pinned VLABench source, after installation:

```bash
git -C "$ASSETS/VLABench" apply --check "$ROBOTENON/extensions/benchmarks/vlabench/patches/lazy-open3d.patch"
git -C "$ASSETS/VLABench" apply "$ROBOTENON/extensions/benchmarks/vlabench/patches/lazy-open3d.patch"
```

Do not reapply to an already patched checkout. Record this upstream modification when reporting results. Model-side configuration enumeration also needs access to the source without importing the simulator:

```bash
export PYTHONPATH="$ASSETS/VLABench${PYTHONPATH:+:$PYTHONPATH}"
export ROBOTENON_LOCAL_RUNTIME_ROOT=/tmp/robotenon-benchmark
```

Model service metrics are stored under `$ROBOTENON_LOCAL_RUNTIME_ROOT/<run-id>/batch-metrics`. Use unique run IDs; local files are not automatically archived to shared storage. Preserve final metrics before recycling the node. Episode results continue to use the evaluator output directory. The model and simulator interpreters remain separate; see the [full-run report](../../../docs/tutorials/benchmark-h100-xiaomi-vlabench.md) for all execution parameters.

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/vlabench/configs/eval.json" "$ROBOTENON/local-profiles/vlabench.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark vlabench --profile "$ROBOTENON/local-profiles/vlabench.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/vlabench.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/vlabench-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
