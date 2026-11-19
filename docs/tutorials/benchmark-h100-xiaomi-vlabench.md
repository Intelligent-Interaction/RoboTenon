# Xiaomi-Robotics-1 on VLABench

[**English**](benchmark-h100-xiaomi-vlabench.md) · [简体中文](benchmark-h100-xiaomi-vlabench.zh-CN.md)

Reproduce the VLABench run using the commands, task results, throughput measurements, and troubleshooting guidance below.
Replace the example deployment paths with local paths.

Xiaomi-Robotics-1 achieved **60.84% task-macro success** across five VLABench tracks with the unified evaluator. Eight H100 GPUs and 160 simulation Workers completed all 2,460 episodes in **1 h 53 min 59 s (about 114 min)**, averaging **21.58 episodes/min** end to end.

The following sections cover hardware, model settings, task results, and parallel execution. See also the [RoboCasa365 report](benchmark-h100-xiaomi.md) and [VLABench asset guide](../../extensions/benchmarks/vlabench/README.md).

## Final results

| Metric | Result |
| --- | ---: |
| Task-macro success rate | **60.84%** |
| Episode-weighted success rate | **61.18%** |
| Completed | 2,460 / 2,460 |
| Successful / unsuccessful | 1,505 / 955 |
| Errors / cancellations | 0 / 0 |
| Wall time | **6,838.54 s, about 1 h 54 min** |
| End-to-end throughput | **21.58 episodes/min** |

Time includes model service startup, simulation, and shutdown. It excludes installation and downloads. All scores come from one complete run.

## Protocol and scoring

Tracks 1, 2, 3, 4, and 6 contain ten task entries each. The robot is `franka`. Each task has up to 50 episodes. In these assets, `insert_flower` in Track 2 has only ten configurations, giving **49 × 50 + 10 = 2,460** episodes. For version and installation details, see [Setup](../../extensions/benchmarks/vlabench/SETUP.md).

The run retained native horizons with no global `max_steps` override. There were 500 episodes with a 100-step limit, 1,710 with a 200-step limit, and 250 with a 300-step limit. These are adapter-level environment actions. MuJoCo integration steps and model requests are counted separately.

- Episode-weighted success rate: `1505 / 2460 = 61.1789%`.
- Task-macro success rate: compute each track/task rate, then average the 50 rates with equal weights to obtain `60.84%`.
- All 955 unsuccessful episodes reached their task horizon. The evaluator recorded zero errors and cancellations.

Unequal episode counts explain the difference between the two rates. Match task configuration counts, horizons, and scoring rules when comparing results.

## Hardware, environments, and execution settings

| Item | Configuration |
| --- | --- |
| GPU | 8 × H100 80 GB, one resident model replica per GPU |
| CPU | 2 × Intel Xeon Platinum 8558, 96 physical cores / 192 logical CPUs |
| Host RAM | About 2 TiB installed. Peak consumption was not measured here. |
| Model environment | Python 3.12, torch 2.7.1+cu128, transformers 5.11.0 |
| Simulation environment | Separate VLABench environment with MuJoCo 3.2.2 and dm_control 1.0.22 |
| Model | `XiaomiRobotics/Xiaomi-Robotics-1-VLABench` |
| Inference | BF16 including full-model conversion, Flash Attention 2 |
| Workers | 20 per GPU, 160 total |
| Batching | Limit 16, wait window 200 ms, 256 pending requests per service |
| Scheduling | `global_queue`, shuffle enabled, scheduler seed 7 |
| Startup concurrency / recycling | `startup_parallelism=64` and `recycle_after_tasks=1000` |
| Retries | `max_retries=1` applies to infrastructure failures. Ordinary task failures are recorded as outcomes. |
| Timeouts | Task 7,200 s, inference 600 s, model and Worker startup 1,800 s each |
| Tracing / history cache | `summary` and history-cache `auto`, effectively off |
| Context limits | 64 history entries and 64 outputs |
| Action timeout | Effective derived value `action_timeout_ms=7260000` |
| Video / step traces | `save_videos=false` and `save_traces=false` |
| Rendering | EGL, 256 × 256 environment images, backend image-processing size 480 |
| Recording options | video_stride 1, video_fps 20 (video off), memory_mode disabled |
| Additional services and overrides | No extra system config, task subset, episode override, or virtual GPU mapping |

Set `model_path` to the downloaded checkpoint path in the backend configuration:

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-1-VLABench",
  "rng_mode": "official_stream",
  "inference_seed": 42,
  "vlabench_image_size": 480,
  "vlabench_cot": false,
  "vlabench_position_offset": [0.0, -0.4, 0.78],
  "vlabench_gripper_threshold": 0.2
}
```

The backend uses history `length=1, interval=1` and infers every five actions. The checkpoint emits 10 × 60 chunks. The adapter uses the first seven dimensions and robot key `vlabench_choice`. It accumulates deltas into end-effector targets within each plan and normalizes angles. The model seed is 42 and the scheduler seed is 7. The environment uses each track's episode configuration with no additional seed override.

Thread and runtime settings:

```bash
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 OMP_THREAD_LIMIT=1 TBB_NUM_THREADS=1
export OPENCV_FOR_THREADS_NUM=1
export MALLOC_ARENA_MAX=1 MALLOC_TRIM_THRESHOLD_=131072
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
export ROBOTENON_LOCAL_RUNTIME_ROOT=/tmp/robotenon-benchmark
ulimit -n 1048576
```

The model process adds the VLABench source root to `PYTHONPATH` to enumerate configurations. The simulator wrapper clears `PYTHONPATH` and loads dependencies from its own environment. Thread variables limit individual library pools.

## Throughput and batching

| Metric | Final value | Basis |
| --- | ---: | --- |
| Completed / successful / unsuccessful | 2460 / 1505 / 955 | Final results |
| Errors / cancellations | 0 / 0 | Final results |
| Wall time | 6,838.54 s | Service startup, execution, and shutdown. Installation and downloads excluded. |
| End-to-end throughput | 21.58 episodes/min | `2460 × 60 / wall_sec` |
| Model requests | 72,229 | Sum across eight model services |
| Batch calls | 47,599 | Sum across eight model services |
| Mean batch size | 1.52 | Requests divided by calls |

| Observed batch size | Calls |
| --- | ---: |
| 1 | 29,373 |
| 2 | 13,222 |
| 3 | 3,884 |
| 4 | 892 |
| 5 | 185 |
| 6 | 35 |
| 7 | 7 |
| 8 | 1 |
| 9–16 | 0 |

The 160 simulation Workers took episodes from a global queue and used eight resident model replicas. Dynamic batching grouped 72,229 requests into 47,599 calls. Request timing limited the mean batch to 1.52 against a maximum of 16. Measuring speedup over serial execution would require a separate control run.

## Results by task

| Track | Successes / total | Episode-weighted SR | Task-macro SR |
| --- | ---: | ---: | ---: |
| 1：in distribution | 375 / 500 | 75.00% | 75.00% |
| 2：cross category | 268 / 460 | 58.26% | 56.80% |
| 3：common sense | 252 / 500 | 50.40% | 50.40% |
| 4：semantic instruction | 270 / 500 | 54.00% | 54.00% |
| 6：unseen texture | 340 / 500 | 68.00% | 68.00% |
| Total | 1505 / 2460 | 61.18% | 60.84% |

Each cell gives successes / episodes. Divide the two for the success rate. The poker and mahjong entries in Track 3 use the task names indicated. Other tracks use the base task names.

| Task | Track 1 | Track 2 | Track 3 | Track 4 | Track 6 |
| --- | ---: | ---: | ---: | ---: | ---: |
| add_condiment | 27/50 | 14/50 | 13/50 | 14/50 | 28/50 |
| insert_flower | 26/50 | 4/10 | 22/50 | 20/50 | 17/50 |
| select_book | 37/50 | 22/50 | 29/50 | 42/50 | 36/50 |
| select_chemistry_tube | 44/50 | 25/50 | 49/50 | 41/50 | 46/50 |
| select_drink | 37/50 | 22/50 | 24/50 | 22/50 | 34/50 |
| select_fruit | 35/50 | 40/50 | 31/50 | 27/50 | 32/50 |
| select_mahjong（Track 3: select_unique_type_mahjong） | 46/50 | 40/50 | 7/50 | 32/50 | 33/50 |
| select_painting | 35/50 | 27/50 | 29/50 | 32/50 | 35/50 |
| select_poker（Track 3: select_nth_largest_poker） | 50/50 | 42/50 | 15/50 | 13/50 | 50/50 |
| select_toy | 38/50 | 32/50 | 33/50 | 27/50 | 29/50 |

## Run the full evaluation

Follow [Setup](../../extensions/benchmarks/vlabench/SETUP.md) to create separate model and simulator environments, install assets and weights, and apply the Open3D lazy-loading patch. The patch avoids importing unused point-cloud dependencies.

Use the repository's [full profile](../../extensions/benchmarks/vlabench/configs/full.json) and save the model JSON above as `local-configs/xiaomi-vlabench.json`. Set `MODEL_PY` and `SIM_PY` to the corresponding interpreters. Apply the thread and renderer settings and run from the repository root:

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark vlabench --profile full \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./local-configs/xiaomi-vlabench.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 20 --batch-size 16 --wait-ms 200 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --startup-parallelism 64 --recycle-after-tasks 1000 --max-retries 1 \
  --task-timeout 7200 --inference-timeout 600 \
  --model-startup-timeout 1800 --worker-startup-timeout 1800 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output runs/xiaomi-vlabench --run-id full-local
```

Start with one GPU and one Worker and add `--max-tasks 1 --episodes-per-task 1` to inspect inputs, actions, and rendering. Remove both subset options for the full evaluation and choose a fresh output directory.

## Read and interpret results

During execution, read `summary` in `status.json`. After completion, inspect `summary.json`. The effective settings are in `protocol.json`. Archive the node-local batch statistics from `$ROBOTENON_LOCAL_RUNTIME_ROOT/<run-id>/batch-metrics` before releasing the nodes. See the [evaluation reference](../../extensions/benchmarks/EVALUATION.md).

This run did not measure between-run variance. Dynamic batching and the `official_stream` random stream can change trajectories with scheduling. The model environment used Transformers 5.11.0, compared with 4.57.1 in Xiaomi's guide. Remeasure after changing dependencies, assets, or execution settings. This report covers task success only. Intention and progress scores are separate metrics.

## Troubleshooting

- **Scoring.** VLABench tasks have different episode counts. The headline result averages
    50 track/task rates equally, giving 60.84%. Weighting by episode gives 61.18%.
- **Horizons.** Native step budgets include 100, 200, and 300 steps. A common higher limit
    changes the protocol, scores, and wall time.
- **Termination.** All 955 unsuccessful episodes reached their native step limit.
    Errors and cancellations were zero. Separate task outcomes from infrastructure failures in diagnostics.
- **Asset coverage.** Install configuration files, meshes, textures, and robot assets. One successful smoke test
    covers only one task. Enumerate the plan and check resource paths for all five tracks before a full run.
- **Repeated startup.** Running tracks separately reloads weights and can leave GPUs idle.
    Where supported, merge profiles, shuffle the episode plan, schedule globally, and aggregate the final results.
- **CPU and memory contention.** This run used 160 Workers. Limit numerical library threads
    and size the pool using sustained episodes/min, RSS, and reset latency.
- **Batch size.** Asynchronous arrivals determine the observed batch histogram. Raising the maximum
    or increasing the collection window can increase action latency with little throughput gain.
- **Rendering.** GPU activity in the model service does not establish that the simulator uses EGL.
    Check the model service, simulator process, and device mapping independently.
- **Dependency versions.** The model environment differs from Xiaomi's reference setup.
    Repeat the smoke test and measurements after changing Transformers, NumPy, or simulator dependencies.
- **Recording.** Full video recording adds encoding and disk I/O. The reported time was measured
    with recording off. Use a small set of failed tasks for video diagnosis.
