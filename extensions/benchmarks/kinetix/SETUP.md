# kinetix: installation, assets and first evaluation

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

## Runtime

Python 3.11; JAX; CPU or CUDA.

Start with the [shared setup](../SETUP.md): define `ROBOTENON`, `ASSETS`, `SIM_PY` and `MODEL_PY`, use an isolated simulator environment, install this extension's requirements, then perform the benchmark-specific steps below. Commands run on the simulator server; downloads do not traverse a local forwarding process. Read the build-order exceptions below **before** installing requirements.

## Assets and simulator setup

Install Kinetix v3.0.0 and download RTC world JSON files (small source assets, not a training dataset).
```bash
git clone https://github.com/Physical-Intelligence/real-time-chunking-kinetix.git "$ASSETS/rtc"
git -C "$ASSETS/rtc" checkout 9296f31d62d5bfeb5779dcb2f9bcf71ca37f448b
"$SIM_PY" -m pip install 'git+https://github.com/FLAIROx/Kinetix.git@v3.0.0'
```
Set `params.rtc_worlds_dir` to `$ASSETS/rtc/worlds` in both sync and realtime profiles. For CPU use `jax` and profile `render: "cpu"`; the requirements file's `jax[cuda12]` extra is for Linux CUDA and must be omitted on macOS/CPU-only installations. For CUDA install the CUDA extra in a compatible driver environment.

The README's realtime section describes live Graph timing and stale actions. First-use JAX compilation can miss the requested Hz; examine `rt_metrics`, not just episode success. Sync and realtime scores are different execution protocols.

## Configure local paths

Copy the profile before changing asset paths; JSON does not expand `$ASSETS` or `~`:

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/kinetix/configs/eval.json" "$ROBOTENON/local-profiles/kinetix.json"
```

Edit the copy's `params` using absolute paths described above. Keep its original task IDs, seed, horizon and observation settings for protocol comparisons. For a quick install check the command below limits tasks/episodes; it may still run to the selected task's complete horizon.

## Smoke with fake actions

From the RoboTenon repository root, after setup:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark kinetix --profile "$ROBOTENON/local-profiles/kinetix.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/kinetix.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/kinetix-install-check"
```

For GPU-required environments add `--model-devices 0` and ensure driver/render access inside the simulator process. Fake actions test the interface; zero policy success is expected and does not indicate a broken environment. Check episode errors, steps, observations and image contents using the [result checklist](../SETUP.md#verify-the-result).

## Next steps

Choose the intended protocol in [README](README.md), then replace fake with your [model backend](../models/README.md). For parallel execution, monitoring and result aggregation use the [evaluation manual](../EVALUATION.md). The smoke subset above is not a published accuracy result.

## Verification scope

A selected-task real simulator action/image/signal probe exists; this is not a fresh clean-machine installation test or full-suite success-rate certification. See the [native parity report](../../../docs/changes/native-action-parity-2026-09-28.md) for task/seed limits.
The [realtime validation](../../../docs/changes/kinetix-live-unified-2026-09-29.md) additionally covers the live Graph on CPU.

Source pins and licenses: [requirements](requirements.txt), [provenance](benchmark-source.json), [native protocol](NATIVE_EVALUATION.md). Do not substitute current upstream main for the recorded revisions when comparing results.
