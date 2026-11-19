# Xiaomi Robotics 0 on LIBERO

[**English**](benchmark-h100-xiaomi-libero.md) · [简体中文](benchmark-h100-xiaomi-libero.zh-CN.md)

Reproduce the base LIBERO evaluation using the results, portable settings,
launch commands, and troubleshooting notes below. Example paths can be replaced for a new installation.
Infrastructure identifiers are omitted.

The run used the checkpoint
[`XiaomiRobotics/Xiaomi-Robotics-0-LIBERO`](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-0-LIBERO)，
and the base LIBERO suites `spatial`, `object`, `goal`, and `10`. For installation and assets, see
[LIBERO setup](../../extensions/benchmarks/libero/SETUP.md). For the model interface, see
[Xiaomi Robotics 0](../../extensions/benchmarks/models/xiaomi-robotics0/README.md)。

## Results at a glance

Eight H100 GPUs and 36 simulation Workers per GPU completed all 2,000 episodes across four suites:

- Overall success rate: **97.15% (1,943 / 2,000)**.
- Wall time: **20 min 35 s**.
- End-to-end throughput: **97.15 episodes/min**.
- Execution errors / cancellations: **0 / 0**.
- All 57 unsuccessful episodes reached their native horizon without meeting the goal.

The success percentage and throughput happen to share the number 97.15. Their units differ.

| Suite | Horizon | Successes / total | Success rate |
| --- | ---: | ---: | ---: |
| `libero_spatial` | 220 | 468 / 500 | 93.60% |
| `libero_object` | 280 | 496 / 500 | 99.20% |
| `libero_goal` | 300 | 491 / 500 | 98.20% |
| `libero_10` | 520 | 488 / 500 | 97.60% |
| **Total** | — | **1,943 / 2,000** | **97.15%** |

Each suite has ten tasks and each task has 50 episodes, so the mean of the four suite rates equals the overall rate.
When episode counts differ, weight by the actual number of episodes.

## Inspect the 57 failures

The run had no model service errors, lost Workers, or cancellations. All 57 failures ended with `horizon`.
The policy had not met the goal by the native step limit.

Failures were concentrated in a few tasks:

| Task | Suite | Successes / 50 | Failures |
| --- | --- | ---: | ---: |
| pick up the black bowl on the ramekin and place it on the plate | spatial | 21 / 50 | 29 |
| open the top drawer and put the bowl inside | goal | 45 / 50 | 5 |
| put the white mug on the plate and put the chocolate pudding to the right of the plate | libero_10 | 45 / 50 | 5 |
| put both moka pots on the stove | libero_10 | 47 / 50 | 3 |
| put the yellow and white mug in the microwave and close it | libero_10 | 47 / 50 | 3 |

Other failures were spread across several tasks. The weakest spatial task accounted for 29 failures, more than half the total.
Reviewing or recording this task exposes behavior hidden by the suite average.
Keep the native horizons when diagnosing this baseline.

## Runtime configuration

| Item | Setting | Interpretation |
| --- | --- | --- |
| GPU | 8 × NVIDIA H100 80 GB | One resident model replica per GPU |
| CPU / RAM | 96 physical cores, 192 logical CPUs, about 2 TiB RAM | Hardware used for this run |
| Simulator Workers | 36 per GPU, 288 total | Start at 1–4 Workers on smaller hosts |
| Protocol | Four merged suites with 50 episodes per task | 40 tasks and 2,000 episodes |
| Scheduling | Global queue, shuffled in advance, seed 7 | Reduces GPU idle time and repeated loading between suites |
| Model batching | Maximum 32, window 100 ms | Observed mean batch size was 1.45 |
| Worker recycling | After 1,000 tasks | No frequent recycling during this run |
| Images | Base and wrist views, 256×256 RGB | The adapter requires the wrist image |
| Actions | Continuous 7-DoF, replan every ten steps | The replanning interval is part of the protocol |
| Recording | Video and full traces off | Enabling them changes runtime and storage needs |

The model environment owns PyTorch, Transformers, the checkpoint, and FlashAttention.
The simulation environment owns LIBERO, robosuite, MuJoCo, and rendering. Keep the environments separate
so that NumPy, MuJoCo, and model dependency conflicts can be diagnosed independently.

At high concurrency, limit numerical library threads so that 288 processes do not each create a large CPU pool:

```bash
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false
export MALLOC_ARENA_MAX=2
```

## From smoke test to full run

Verify the inputs, actions, and success signal before increasing the pool to 288 Workers:

```bash
export MODEL_PY=/opt/envs/xiaomi-libero/bin/python
export SIM_PY=/opt/envs/libero/bin/python

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero --profile smoke_test \
  --backend ./extensions/benchmarks/models/xiaomi-robotics0/vla.py \
  --backend-config ./local-configs/xiaomi-robotics0-libero.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 \
  --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/xiaomi-robotics0-libero-smoke
```

Check that `status.json` reports no errors, episodes advance, and both images have the expected dimensions. Then run the full protocol:

```bash
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false MALLOC_ARENA_MAX=2
export EVAL_OUTPUT="runs/xiaomi-robotics0-libero-$(date +%Y%m%d-%H%M%S)"

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero --profiles spatial object goal 10 \
  --backend ./extensions/benchmarks/models/xiaomi-robotics0/vla.py \
  --backend-config ./local-configs/xiaomi-robotics0-libero.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 36 --batch-size 32 --wait-ms 100 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --episodes-per-task 50 --startup-parallelism 8 \
  --max-retries 1 --task-timeout 14400 --inference-timeout 600 \
  --model-startup-timeout 1800 --worker-startup-timeout 1800 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output "$EVAL_OUTPUT"
```

Set the checkpoint path in your local model configuration:

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-0-LIBERO",
  "device": "cuda",
  "metrics_dir": "/data/runs/xiaomi-robotics0/metrics"
}
```

## Observed batching and speed

The limit of 32 is a ceiling. The run combined 30,714 requests into 21,169 calls, with an average
batch size of 1.45. This reduced the model call count by 31.08%. End-to-end speed includes other work.

| Observed batch size | Calls | Share of calls |
| ---: | ---: | ---: |
| 1 | 13,555 | 64.03% |
| 2 | 5,997 | 28.33% |
| 3 | 1,338 | 6.32% |
| 4 | 248 | 1.17% |
| 5 | 27 | 0.13% |
| 6 | 4 | 0.02% |
| 7–32 | 0 | 0% |

A model call averaged about 175 ms. Request arrival depends on simulation steps, early successes, and reset time.
Increasing `batch-size` alone cannot guarantee larger batches. Check Worker count, CPU contention,
and the observed batch histogram when GPU utilization is low.

## Troubleshooting

### 1. Running a single suite

`--profile spatial` covers 500 episodes. To evaluate all four suites, use
`--profiles spatial object goal 10` to create one plan, shuffle and aggregate across suites, and keep the model
service running throughout.

### 2. Interpreting `completed` as success

`completed` means the episode finished normally. Read `task_success` to determine whether the policy met the goal. All 2,000 episodes
completed here, including 57 horizon failures.

### 3. Extending the horizon

A larger horizon changes the protocol. Check the failing tasks, camera inputs, action transforms,
and gripper conventions first. Use a separate, labeled profile to study a different step budget.

### 4. Conflicting model and simulation environments

LIBERO forks share the import name `libero`, while the model stack has its own PyTorch and Transformers constraints.
Give each fork its own environment and LIBERO configuration. Confirm which source tree `SIM_PY` imports.

### 5. Missing online assets after downloading training data

Online evaluation uses BDDL, initial states, meshes, and textures. It does not require the training demonstration HDF5 files.
Install the simulation assets listed in the setup guide.

### 6. Copying the 36-Worker configuration to a smaller host

High concurrency needs substantial CPU and memory. Too many Workers can slow resets, increase peak RAM, or exhaust memory.
Try one, four, and eight Workers per GPU in turn, recording episodes/min, RSS, and GPU utilization before increasing the pool.

### 7. Troubleshooting headless rendering

Servers usually need `MUJOCO_GL=egl`. If Workers start but GPUs show no rendering load, check EGL mapping,
container driver access, and the simulator's Python interpreter. The model and simulator use separate GPU paths.

### 8. Measuring the cost of video and tracing

The reported speed uses no video or full traces. Record a small number of tasks for diagnosis. Recording all episodes
adds encoding and disk I/O. Measure that configuration separately from the 20 min 35 s baseline.

## Scope of the results

- One full run was recorded. Multiple scheduler seeds and confidence intervals were outside its scope.
- Dynamic batching, BF16, and multiple replicas can change timing and numerical results across machines.
- These measurements use the specified dependencies and protocol. When comparing another implementation, match task selection,
    episodes per task, initial states, horizons, cameras, and action chunks.
- The setup guide lists the public dependencies and asset sources needed to reproduce the evaluation.
