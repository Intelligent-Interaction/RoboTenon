# OpenVLA-OFT / LIBERO-Spatial

[**English**](README.md) · [简体中文](README.zh-CN.md)

This backend integrates the original LIBERO-Spatial suite and official Spatial checkpoint. It accepts one request per call (`supports_batch=False`, `batch-size` 1). It solved 491 of 500 episodes (98.20%) under the protocol below. The official four-suite average requires additional checkpoints and suites.

## Files and call sequence

| File | Responsibility |
| --- | --- |
| `vla.py` | Lightweight SPEC, observation parameters, and create factory. Reading the declaration does not import Torch or TensorFlow. |
| `implementation.py` | Sessions, input validation, official inference calls, and conversion to native actions |
| `configs/libero-spatial.json` | Portable path examples and model seed. Copy and update for the deployment. |
| `SOURCE.json` | Pinned official code, dependency forks, and checkpoint revision |
| `../../libero/configs/openvla_oft_spatial.json` | Matching task/environment interface and protocol |

Inference follows `evaluate → BackendRuntime → VLAProvider → OpenVLAOFT.act_batch`. Actions execute through `BenchmarkPolicy → Body → LIBEROBenchmark.step`. The framework queues the eight-step chunk and discards remaining actions after success. The adapter does not run an independent environment loop.

## Inputs and actions

- `open_session(session_id, task_info)` accepts only `benchmark_id=libero_benchmark` and `task.suite=libero_spatial`. Other suites require their own weights.
- `act_batch` accepts one request at a time and returns its `request_id`.
- `observation.history` has length 1. `images.agentview` and `images.wrist` are 256×256 uint8 RGB. The environment already flips both axes, so the adapter preserves their orientation.
- `states` contains `[eef_pos3, axisangle3, gripper_qpos2]`. The adapter copies it before official checkpoint normalization to protect the framework cache.
- Official `get_vla_action` handles JPEG encoding/decoding, Lanczos3 resize to 224, center crop, prompt construction, lowercase task text, processing, and prediction. It is used directly rather than approximated with PIL.
- Returns an 8×7 action chunk. The first six dimensions pass through unchanged. The gripper uses `-sign(2*g-1)`, matching official normalization, binarization, and inversion. At g=0.5 this returns 0, so the profile sets `discretize_gripper=false`.
- The profile retains seed=7, env_seed=0, ten initialization steps, delta control, and the original budget of 220 actions. Initialization is outside the action budget. Episode index selects the initial state.
- The framework's `quat_no_antipodal` path casts axis-angle values to float32. The standalone official evaluator may retain higher precision. The recorded run used the framework implementation. Bitwise equivalence of inputs or trajectories across evaluators is unverified.

## Validation results

On 2026-10-18 the full LIBERO-Spatial run evaluated ten tasks and predefined initial states 0–49 for each, totaling 500 episodes. It solved 491 (98.20%). Nine reached the native horizon without success. No technical errors or unfinished episodes were recorded.

The run used the pinned Spatial weights and source in `SOURCE.json`, seed=7, env_seed=0, ten initialization steps, 220 native actions, eight actions per prediction, one A100, one worker, batch=1, and no automatic retries. BF16/SDPA inference produced 6,684 predictions and 51,703 executed actions.

Acceptance checks covered sample/session uniqueness, raw predictions, processor inputs, request/observation association, executed actions and gripper conversion, file hashes, terminal observations, native video, and tracing. Raw data and verification materials were retained in the internal run bundle. That run included separate fixes to video, terminal observation, and trace recording. These fixes and internal evidence scripts are outside the model adapter. The commands below do not recreate the exact recording setup. The adapter does not automatically export processor tensors or raw prediction copies.

This is a single-seed Spatial result using fixed weights and the RoboTenon executor. This adapter has not integrated Object, Goal, LIBERO-10, or other benchmarks. Multi-seed statistics, batching performance, and a paired reproduction with the official evaluator remain unverified.

## Environment preparation

Use Python 3.10 for the model and a separate simulator environment. Replace the example paths with actual local paths. Training demonstrations are unnecessary.

```bash
git clone https://github.com/moojink/openvla-oft.git /opt/src/openvla-oft
git -C /opt/src/openvla-oft checkout e4287e94541f459edc4feabc4e181f537cd569a8

conda create -p /opt/envs/openvla-oft python=3.10 -y
MODEL_PY=/opt/envs/openvla-oft/bin/python
ROBOTENON=/path/to/RoboTenon

# Select a Hugging Face mirror if needed. Inference requires a complete local checkpoint.
export HF_ENDPOINT=https://hf-mirror.com
"$MODEL_PY" -m pip install -r "$ROBOTENON/extensions/benchmarks/models/openvla-oft/requirements.txt"
"$MODEL_PY" -m pip install --no-deps -e /opt/src/openvla-oft
"$MODEL_PY" -m pip install -e "$ROBOTENON" -e "$ROBOTENON/extensions/benchmarks/benchmark-support"
```

`requirements.txt` pins Torch 2.2, two Git forks, and the validated compatibility constraints `tensorflow-metadata==1.15.0` and `wandb==0.16.6`. Other ranges do not form a complete lockfile. Verify installation and imports in a fresh environment. Use the official Transformers fork for bidirectional attention. The backend uses SDPA and needs no separate FlashAttention package.

Prepare LIBERO following its [setup guide](../../libero/SETUP.md). Set `SIM_PY` to its interpreter and install the framework, benchmark-support, and LIBERO extension there. The guide and the official OFT script use different robosuite versions. This integration preserves the existing benchmark dependencies. Record source revisions and environment manifests for cross-version comparisons.

Download the checkpoint revision in `SOURCE.json`, verify that the weights are complete, and configure an absolute local path. The adapter loads the local model and action/proprio heads read-only. It bypasses the official `get_vla()` helper, which can modify the checkpoint.

## CPU interface checks

Run in an environment with NumPy, Pillow, pytest, and the RoboTenon core dependencies:

```bash
cd /path/to/RoboTenon
CUDA_VISIBLE_DEVICES="" python -m pytest -q \
  extensions/benchmarks/benchmark-support/tests/test_openvla_oft_adapter.py
```

The checks use simulated predictions for declaration loading, profile settings, observations/state, action conversion, session/request association, and no-retry error handling. They run without Torch, TensorFlow, weights, or a simulator. Validate real inference separately.

## Run example

After editing paths in a copy of the backend JSON, this command runs **one task × one episode on one worker, batch 1, up to 220 actions, without retries**. A full episode can require up to 28 predictions. Success can end it earlier.

```bash
cd /path/to/RoboTenon
MODEL_PY=/opt/envs/openvla-oft/bin/python
SIM_PY=/opt/envs/libero/bin/python
# Copy the configuration and set model_path and upstream_path in the copy.
mkdir -p /data/configs
cp ./extensions/benchmarks/models/openvla-oft/configs/libero-spatial.json \
  /data/configs/openvla-oft-spatial.json
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero --profile openvla_oft_spatial \
  --backend ./extensions/benchmarks/models/openvla-oft/vla.py \
  --backend-config /data/configs/openvla-oft-spatial.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 --max-retries 0 --no-shuffle \
  --output "runs/openvla-oft-spatial-$(date +%Y%m%d-%H%M%S)"
```

On first deployment, use the single episode to check weights, both images, state, actions, and termination. The full profile has ten tasks × 50 episodes. Remove `--max-tasks 1 --episodes-per-task 1` and select a new output directory for the complete Spatial evaluation.

## Official sources

- [Code and dependencies](https://github.com/moojink/openvla-oft/tree/e4287e94541f459edc4feabc4e181f537cd569a8)
- [LIBERO evaluation](https://github.com/moojink/openvla-oft/blob/e4287e94541f459edc4feabc4e181f537cd569a8/LIBERO.md)
- [Native input and action pipeline](https://github.com/moojink/openvla-oft/blob/e4287e94541f459edc4feabc4e181f537cd569a8/experiments/robot/libero/run_libero_eval.py)

The official 97.1% is the mean across four suites with separate checkpoints. This adapter implements Spatial only.
