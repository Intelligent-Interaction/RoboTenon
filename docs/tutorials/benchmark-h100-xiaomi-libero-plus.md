# Xiaomi Robotics 0 on LIBERO-Plus

[**English**](benchmark-h100-xiaomi-libero-plus.md) · [简体中文](benchmark-h100-xiaomi-libero-plus.zh-CN.md)

Use this guide to reproduce or extend the LIBERO-Plus evaluation.
It covers launch settings, score interpretation, low success rates, GPU utilization, and episodes that reach their horizon.
All example paths are portable.

The four LIBERO-Plus suites were evaluated through the unified runner using
[`XiaomiRobotics/Xiaomi-Robotics-0-LIBERO`](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-0-LIBERO)
with the settings below. The checkpoint was released for base LIBERO. This experiment measures transfer
under visual, language, layout, and sensor perturbations. The reported score is specific to this run.
The base LIBERO protocol and Xiaomi's published results use a different evaluation set.

For installation and assets, see [LIBERO-Plus setup](../../extensions/benchmarks/libero_plus/SETUP.md).
For the model adapter, see [Xiaomi Robotics 0](../../extensions/benchmarks/models/xiaomi-robotics0/README.md).

## Results at a glance

The full run finished on **October 10, 2026**. **Eight H100 GPUs completed 10,030 cases in 2 h 7 min 57 s, with 7,499 successes and a success rate of
74.77%.** End-to-end throughput averaged **78.39 episodes/min**. No execution errors or cancellations occurred.

| Metric | Result | Definition |
| --- | ---: | --- |
| Completed | 10,030 / 10,030 | All enumerated cases in four suites, one episode per case |
| Successes / task failures | 7,499 / 2,531 | Based on the environment's final success signal |
| Success rate | 74.77% | 7,499 ÷ 10,030, with equal weight per case |
| Errors / cancellations | 0 / 0 | Final evaluator counts |
| Wall time | 7,677.13 s, about 127.95 min | Model startup, Worker execution, and shutdown. Downloads and installation excluded. |
| End-to-end throughput | 78.39 episodes/min | Completed episodes divided by wall time |
| Model requests / batch calls | 201,028 / 137,997 | Sum of the final counters from eight model services |
| Mean batch size | 1.46 | Requests divided by calls, with a configured maximum of 32 |

### Results by suite

| Suite | Horizon | Successes / total | Success rate |
| --- | ---: | ---: | ---: |
| `libero_spatial` | 220 | 1,971 / 2,402 | 82.06% |
| `libero_object` | 280 | 1,925 / 2,518 | 76.45% |
| `libero_goal` | 300 | 1,775 / 2,591 | 68.51% |
| `libero_10` | 520 | 1,828 / 2,519 | 72.57% |
| **Total** | — | **7,499 / 10,030** | **74.77%** |

Suite sizes differ. The overall score weights each suite by its episode count.

### Results by perturbation

| Perturbation | Successes / total | Success rate |
| --- | ---: | ---: |
| Background Textures | 994 / 1,076 | 92.38% |
| Camera Viewpoints | 658 / 1,599 | 41.15% |
| Language Instructions | 1,376 / 1,537 | 89.53% |
| Light Conditions | 1,059 / 1,142 | 92.73% |
| Objects Layout | 1,151 / 1,525 | 75.48% |
| Robot Initial States | 876 / 1,550 | 56.52% |
| Sensor Noise | 1,385 / 1,601 | 86.51% |

Camera viewpoint changes had the lowest success rate, followed by changes to the robot's initial state.
Rates were higher for backgrounds, lighting, instruction wording, and sensor noise. These differences describe this checkpoint
under the selected protocol. They do not isolate effects of the architecture or training data.

### Results by difficulty

| Difficulty | Successes / total | Success rate |
| --- | ---: | ---: |
| 1 | 1,494 / 1,644 | 90.88% |
| 2 | 1,872 / 2,202 | 85.01% |
| 3 | 1,668 / 2,094 | 79.66% |
| 4 | 1,366 / 1,886 | 72.43% |
| 5 | 994 / 2,083 | 47.72% |
| Unlabeled | 105 / 121 | 86.78% |

Success generally decreased as annotated difficulty rose, reaching 47.72% at level 5. Difficulty levels contain different numbers of cases.
All 10,030 episodes contribute to the overall score.

### Termination reasons

| Reason | Count | Meaning |
| --- | ---: | --- |
| `environment_terminal` | 7,499 | The environment reported task success and ended normally |
| `horizon` | 2,531 | The task remained incomplete at its native suite horizon |
| Execution error | 0 | Model service, simulator Worker, or protocol error |
| Cancelled | 0 | Cancelled episode |

`horizon` records an ordinary task failure. Each suite retained its native horizon.
Episodes ended immediately on success.

## Hardware and runtime settings

| Item | Configuration |
| --- | --- |
| GPU | 8 × NVIDIA H100 80GB HBM3 |
| CPU | 2 × Intel Xeon Platinum 8558, 96 physical cores and 192 logical CPUs |
| RAM | Approximately 2 TiB |
| Model replicas | One per GPU, eight total |
| Simulation Workers | 36 per GPU, 288 total |
| Batching | Up to 32 requests per replica, 100 ms collection window |
| Scheduling | Four profiles, one global shuffled queue, scheduler seed 7 |
| Worker recycling | Rebuild after every eight tasks |
| Video / full traces | Both disabled. Summary tracing enabled. |
| Model input | Agent and wrist views, both 256×256 RGB, and eight-dimensional proprioception |
| Model output | Continuous 7-DoF actions, replanning after at most ten steps |

After the run, the simulation environment reported Python 3.12, NumPy 1.26, robosuite 1.4, and MuJoCo 3.8.
The model environment used Python 3.12, PyTorch 2.7, and Transformers 4.57, with bfloat16 and FlashAttention 2.
For a reproducible installation, use the pinned dependencies in the repository
and the [setup guide](../../extensions/benchmarks/libero_plus/SETUP.md).

The run limited numerical libraries to one thread each to reduce contention among 288 simulator processes:

```bash
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false
export MALLOC_ARENA_MAX=2
```

These settings limit the libraries' thread pools. The operating system still schedules the whole process.

## Protocol and model adapter

The runner merges `all-0`, `all-1`, `all-2`, and `all-3` into one episode plan.
They cover `libero_spatial`, `libero_object`, `libero_goal`, and `libero_10` respectively. Each enumerated case
runs one episode with environment seed 7. After reset, the simulator waits ten environment steps before control begins.

The Xiaomi Robotics 0 backend follows the official LIBERO inference conventions:

- Use the base and left-wrist camera views and the official prompt template.
- Pad the eight-dimensional simulator state to the checkpoint's 32-dimensional state.
- Derive a stable seed from each observation, preserving independent noise for requests in a dynamic batch.
- Return continuous 7-DoF action chunks and replan after ten steps.
- Use one frame of model history. Incremental history caching was disabled for this run.

These settings preserve the model interface. The official base LIBERO evaluator uses a different benchmark protocol.
LIBERO-Plus adds perturbations and changes the task distribution and scoring set.

## Parallel efficiency and batch sizes

Dynamic batching grouped 201,028 inference requests into 137,997 model calls, avoiding 63,031 calls,
a reduction of 31.35%. Wall time also includes simulation, resets, process recycling,
and CPU scheduling.

| Observed batch size | Calls | Share of calls |
| ---: | ---: | ---: |
| 1 | 88,542 | 64.16% |
| 2 | 38,197 | 27.68% |
| 3 | 9,285 | 6.73% |
| 4 | 1,669 | 1.21% |
| 5 | 268 | 0.19% |
| 6 | 31 | 0.02% |
| 7 | 5 | <0.01% |
| 8–32 | 0 | 0% |

Although the configured limit was 32, the largest observed batch was seven and the mean was 1.46. Simulator requests
arrived at different times and rarely filled a larger batch within 100 ms. When tuning throughput,
compare Worker count, wait time, GPU utilization, CPU contention, and trajectory consistency together.

## Run the full protocol

Prepare separate model and simulator environments using the setup guide. From the repository root, adapt the example paths below:

```bash
export MODEL_PY=/opt/envs/xiaomi-libero/bin/python
export SIM_PY=/opt/envs/libero-plus/bin/python
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false MALLOC_ARENA_MAX=2
export EVAL_OUTPUT="runs/xiaomi-robotics0-libero-plus-$(date +%Y%m%d-%H%M%S)"

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_plus --profiles all-0 all-1 all-2 all-3 \
  --backend ./extensions/benchmarks/models/xiaomi-robotics0/vla.py \
  --backend-config ./local-configs/xiaomi-robotics0-libero.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 36 --batch-size 32 --wait-ms 100 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --episodes-per-task 1 --startup-parallelism 8 \
  --recycle-after-tasks 8 --max-retries 1 \
  --task-timeout 14400 --inference-timeout 600 \
  --model-startup-timeout 1800 --worker-startup-timeout 1800 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output "$EVAL_OUTPUT"
```

Point the local model configuration at the downloaded checkpoint. Keep machine-specific paths in local files:

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-0-LIBERO",
  "device": "cuda",
  "metrics_dir": "/data/runs/xiaomi-robotics0/metrics"
}
```

For a first deployment, start with one GPU, one Worker, batch size one, and
`--max-tasks 1 --episodes-per-task 1`. Increase concurrency gradually and monitor CPU utilization, system RAM,
GPU memory, and sustained throughput.

## Troubleshooting

### 1. Interpreting base LIBERO scores

Both protocols can use the same checkpoint. Base LIBERO evaluates fixed tasks and initial states.
LIBERO-Plus includes camera, texture, lighting, language, layout, robot-state, and sensor-noise variants.
The 74.77% reported here and [97.15% on base LIBERO](benchmark-h100-xiaomi-libero.md) measure performance on different evaluation sets.

### 2. Running only `all-0`

`all-0` includes only `libero_spatial`. Use
`--profiles all-0 all-1 all-2 all-3` to cover all four suites. The runner shuffles and aggregates the merged profiles in one global queue.
This avoids repeatedly restarting the model and leaving GPUs idle between suites.

### 3. Importing the wrong LIBERO fork

Base LIBERO, LIBERO-Plus, and LIBERO-Pro can all install a Python package named `libero`.
Use separate simulator environments and LIBERO configuration files. If task counts, asset paths,
or class signatures look wrong, use `SIM_PY` to print the actual module path.

### 4. Missing assets after downloading demonstrations

Online evaluation needs BDDL files, initial states, meshes, textures, and LIBERO-Plus variant configurations.
Training demonstrations are optional. Follow the setup guide's asset list.

### 5. Handling horizon terminations

All 2,531 unsuccessful cases reached their native suite horizon. Infrastructure errors were zero.
Raising every horizon to 1,000 changes the protocol and can substantially lengthen the slowest runs. Group failures by category and task first.

### 6. Low GPU utilization with a maximum batch size of 32

In this run, 64.16% of calls still used batch size one, and the observed maximum was seven. A higher limit
cannot synchronize simulator requests. Inspect CPU contention, simulation speed, resets, and `wait-ms`
alongside `batch-size`.

### 7. Sizing the Worker pool

The 36 Workers per GPU ran on 96 physical CPU cores and about 2 TiB of RAM. More Workers increase peak memory, context switching,
and reset contention, and can exhaust memory. On a new machine, increase concurrency in steps and measure
sustained episodes per minute.

### 8. Confirming GPU rendering

Model CUDA and MuJoCo rendering are separate. Headless operation usually requires `MUJOCO_GL=egl`. Ensure that each simulator
process can see its assigned EGL device. If only the first GPU is busy, inspect Worker bindings and the render backend
alongside PyTorch CUDA status.

### 9. Comparing runs with video enabled

This run disabled video and full tracing. Recording a few failures helps diagnosis. Recording every episode adds encoding
and disk I/O, so its runtime requires a separate measurement from the reported 127.95 minutes.

### 10. Accounting for errors in the denominator

Report planned and completed episodes, successes, errors, and cancellations. This run had zero errors and cancellations.
If a different run has errors, disclose how those episodes enter the denominator.

## Scope of the results

- Each enumerated case ran once. No repeated seeds or confidence intervals were collected.
- Shuffling and dynamic batching change request timing. Per-observation seeds stabilize noise, while floating-point arithmetic
    and simulator scheduling can still vary across hardware.
- Video recording was disabled. Measure throughput separately when recording.
- The final 74.77% is episode-weighted across all LIBERO-Plus cases. The four suites have different sizes.
    Xiaomi's published base LIBERO score uses another protocol.
- Report changes to hardware, dependencies, checkpoint, horizon, camera configuration, or protocol subset as a separate experiment.
