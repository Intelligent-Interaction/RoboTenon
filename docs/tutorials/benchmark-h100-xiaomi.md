# Xiaomi on RoboCasa365

[**English**](benchmark-h100-xiaomi.md) · [简体中文](benchmark-h100-xiaomi.zh-CN.md)

Reproduce the Xiaomi + RoboCasa365 evaluation under the target50 × 50 protocol. This guide covers results,
hardware capacity, launch settings, scoring, and troubleshooting. For installation and assets, see the
[benchmark guide](../benchmarks.md) and [RoboCasa365 setup](../../extensions/benchmarks/robocasa365/SETUP.md).
Examples use portable deployment paths.

## Hardware and parallelism

| Item | Reference setting | Selection guidance |
| --- | --- | --- |
| GPU | 8 × H100 80 GB | Validate model, inference, and render memory on one GPU before scaling up |
| CPU | 96 physical cores, 192 logical CPUs | Size the Worker pool for simulator CPU use and thread count |
| RAM | About 2 TiB | Allow headroom for per-Worker scenes, textures, and reset peaks |
| Model replicas | One per GPU, eight total | Compare throughput and memory before adding replicas |
| Simulation Workers | 28 per GPU, 224 total | Increase gradually while throughput improves and resources permit |

Workers draw episodes from a global queue. Each model service handles requests from its assigned Workers and builds batches independently. Fixed environment seeds do not establish identical actions across schedules. The `official_stream` RNG depends on request order, batching, and replica count.

## Results and parallel efficiency

**Eight H100s completed 2,500 episodes in 1 h 46 min 42 s, with 57.56% success.** End-to-end throughput averaged **23.43 episodes/min**. Sustained throughput in the middle of the run was **26.97 episodes/min**. The run used eight model replicas, 224 Workers, a batch limit of eight, a 50 ms collection window, and no video recording.

| Metric | Result | Definition |
| --- | ---: | --- |
| Completed | 2,500 / 2,500 | 50 tasks × 50 episodes, with no missing or duplicate identities |
| Successes / task failures | 1,439 / 1,061 | Based on the final episode success signal |
| Overall success rate | 57.56% | 1,439 ÷ 2,500. Equal task sizes also make this the mean task success rate. |
| Errors / cancellations | 0 / 0 | Final num_errors / num_cancelled |
| Wall time | 6,401.88 s, about 106.70 min | Evaluator wall_sec includes service startup, Worker execution, and shutdown. Downloads and installation excluded. |
| End-to-end throughput | 23.43 episodes/min | 2,500 divided by total minutes |
| Sustained throughput | 26.97 episodes/min | 2,000 episodes in 4,448.81 s between completions 250 and 2,250 |
| Model requests / batch calls | 206,674 / 88,766 | Sum of counters from eight model services |
| Mean batch size | 2.33 | Requests divided by calls, with a configured maximum of eight |

### Sources of throughput

- **Parallel simulation.** 224 Workers and eight model services run concurrently. Each replica loads weights once and reuses them across episodes.
- **Dynamic batching.** 206,674 requests required 88,766 calls, **117,908 fewer (57.05%)** than one call per request. This measures the reduction in call count. End-to-end speedup requires a timed comparison.
- **Global scheduling.** An idle Worker takes the next episode, reducing idle time caused by differences in task length.
- **Shared runtime costs.** Thread limits, summary tracing, and bounded checkpoints limit CPU and recording costs. Incremental history caching was disabled for this configuration.

Together these settings completed the protocol in under two hours. No full batch-size-one control was run under identical conditions, so the measurements do not isolate any individual optimization or quantify speedup against the upstream evaluator.

### Scope of the results

This was a single complete run. Dynamic batching and `official_stream` can change trajectories. The installed dependencies differ from repository defaults. Sustained throughput excludes the first and last 10% of completions. Use full wall time for end-to-end comparisons and remeasure after changes to hardware, task distribution, observations, or recording.

[Download the result data](../assets/benchmark-results/robocasa365-xiaomi-h100.json) for the protocol, hardware settings, 50 task counts, full totals, and batch histogram. These aggregate data come from the final summary, episode index, and model service counters. All eight services recorded a clean shutdown.

## Model and protocol

At the repository root, create `local-configs/xiaomi.json` and replace the sample checkpoint path with the path visible to your model process:

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-1-RoboCasa365",
  "rng_mode": "official_stream"
}
```

| Setting | Meaning | Selection guidance |
| --- | --- | --- |
| `model_path` | Downloaded checkpoint directory | Use the absolute path to RoboCasa365-compatible weights |
| `rng_mode` | Model randomness | Use `official_stream` for official-stream comparisons, or document `session_deterministic` for scheduling experiments |
| `target50 × 50` | 50 tasks with 50 episodes each | Full protocol: 2,500 episodes. Label subset runs separately. |
| `split=pretrain` | Scene split | Keep the profile split for protocol comparisons |
| `seed=7`, stride 50 | Environment seed is `7 + original_task_index × 50 + episode_idx` | Preserve original indices and protocol seeds independently of Worker ID |
| `camera_size=256` | Environment image dimensions | Keep 256 for the full evaluation |
| Task horizon | Native environment step budget | Retain the task limit and terminate on success |
| History `length=4`, `interval=2` | Four model input frames sampled every two environment steps | Set the SPEC to the selected model's training and inference conventions |
| `replan_steps=16` | Replan after consuming at most 16 actions | Declared by the backend. Changing it changes control behavior. |

## Run the full protocol

Run the following commands at the repository root after installing the model and simulator. Set `MODEL_PY` to the model interpreter and `SIM_PY` to the simulator interpreter. A simulator wrapper script can provide additional rendering settings.

```bash
export MODEL_PY=/opt/envs/model/bin/python
export SIM_PY=/opt/envs/robocasa365/bin/python
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false MALLOC_ARENA_MAX=2
export EVAL_OUTPUT="runs/robocasa365-xiaomi-$(date +%Y%m%d-%H%M%S)"

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./local-configs/xiaomi.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 28 --batch-size 8 --wait-ms 50 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --startup-parallelism 16 --recycle-after-tasks 5 --max-retries 1 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output "$EVAL_OUTPUT"
```

### Command-line options

| Option | Purpose | Selection guidance |
| --- | --- | --- |
| `--benchmark robocasa365` | RoboCasa365 environment adapter | Match the model's observation and action spaces |
| `--profile xiaomi_official` | Full target50 × 50 protocol | Use subset overrides only for small checks |
| `--backend` | Model factory `vla.py` | Use the Xiaomi backend file or provide a custom backend |
| `--backend-config` | JSON parameters for the model factory | Set checkpoint and RNG independently of protocol and resources |
| `--model-python` | Coordinator and model service interpreter | Select the Python installation with model dependencies |
| `--sim-python` | Simulator interpreter or wrapper | Select the installation with working simulation and rendering |
| `--model-devices` | GPUs for inference and render binding | Select available devices. For a single-GPU smoke test use `0`. |
| `--workers-per-device 28` | Simulator Workers per device | Try 1, 4, then 8 and compare throughput and peak memory. Multiply by device count for the total. |
| `--batch-size 8` | Max batch per model replica | Begin at one. For true batching, try 2, 4, and 8 and inspect VRAM and the observed batch histogram. |
| `--wait-ms 50` | Batch collection window, in ms | Start at 50. Single-sample runs can use zero. A longer window adds waiting time. |
| `--scheduling-strategy global_queue` | Idle Workers claim the next episode | Use global scheduling for variable episode lengths. Use static for fixed-assignment comparisons. |
| `--scheduler-seed 7` | Independent seed for initial shuffling | Record an integer and keep it fixed across comparisons |
| `--startup-parallelism 16` | Concurrent startup and reset limit | Size for peak initialization resources. Start at 1–8 on smaller machines. |
| `--recycle-after-tasks 5` | Rebuild each Worker after five tasks | Lower the limit for memory growth. Raise it to reduce restart cost when memory is stable. |
| `--max-retries 1` | One retry for WorkerLost | Use one ordinarily, or zero to diagnose the initial fault. Task failures are not retried. |
| `--trace-mode summary` | Retain summary spans and errors | Use summary for evaluation and full for a detailed timeline |
| `--history-cache auto` | Detect overlapping model history | With auto, Xiaomi's 4/2/16 settings have no overlap and disable incremental caching |
| `--context-history-limit 64` | Diagnostic events retained per checkpoint | Default 64. Increasing this changes the diagnostic window only. |
| `--context-outputs-limit 64` | Recent node outputs retained per checkpoint | Default 64. Increasing this adds checkpoint overhead. |
| `--output` / `EVAL_OUTPUT` | Run output root | Choose a new directory. A timestamp helps avoid overwriting a previous run. |

`batch-size=8` sets a maximum of eight requests. `wait-ms=50` covers only the collection window. Shuffling is enabled by default. `--no-shuffle` disables the initial shuffle and preserves episode identity and environment seeds.

### Thread settings

| Variable | Purpose | Selection guidance |
| --- | --- | --- |
| `OMP_NUM_THREADS` | OpenMP thread pool size | Start at one with many Workers. Test higher values on low-concurrency workloads. |
| `MKL_NUM_THREADS` | MKL thread count | Start at one in multi-process runs to avoid nested pools |
| `OPENBLAS_NUM_THREADS` | OpenBLAS threads | Start at one and tune for total throughput |
| `NUMEXPR_NUM_THREADS` | NumExpr expression threads | Start at one with multiple processes |
| `TOKENIZERS_PARALLELISM` | Tokenizer parallelism | Start with false. Compare alternatives if tokenization limits throughput. |
| `MALLOC_ARENA_MAX` | Maximum glibc allocation arenas | On Linux, try two and inspect RSS and allocation overhead. Other allocators may differ. |

These variables configure library pools and allocator behavior. See the [CLI reference](../../extensions/benchmarks/EVALUATION.md#8-cli-options) for timeouts, virtual GPUs, EGL mapping, and other options.

## Smoke test and full protocol

For a first run, use one GPU, one Worker, batch size one, wait zero, and `--max-tasks 1 --episodes-per-task 1`. The two subset options limit the run to one task and one episode. Check actions, images, and termination. Remove both options and use a new output directory for the full protocol.

For a long evaluation, use a persistent terminal session on the server. For example, run `tmux new -s xiaomi-eval` before the evaluation command. Replace `-s` with an available session name.

## Monitor progress and read results

```bash
"$MODEL_PY" -m json.tool "$EVAL_OUTPUT/protocol.json"
"$MODEL_PY" -m json.tool "$EVAL_OUTPUT/runs/evaluation/status.json"
nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total --format=csv
```

Use `protocol.json` to verify the effective protocol, overrides, and execution settings. `status.json` shows live progress. The query fields in `nvidia-smi` give the device ID, GPU utilization, and used and total memory. CSV output makes devices easier to compare. Instantaneous utilization alone cannot establish sustained throughput.

For recent throughput, see the [SQLite progress query](../benchmarks.md#5-inspect-progress-speed-and-results). Read the observed batch histogram from `batch-metrics/*.json` and the final score from `runs/evaluation/summary.json`. Verify all 2,500 results and the error and cancellation counts.

Successful episodes often end early, while unsuccessful ones run to the horizon. Use recent steady-state throughput to estimate remaining time and allow for long episodes at the end.

## Troubleshooting

- **Worker count.** The 224-Worker configuration was measured on 96 physical cores and about 2 TiB RAM.
    Start at lower concurrency on a new machine and watch for CPU or memory contention.
- **Episode completion.** `completed` also includes episodes that reached their horizon. Count successes using
    `task_success` and report errors and cancellations alongside them.
- **Partial success rates.** Early completions often favor successful episodes because they can terminate sooner.
    Use the final 2,500-episode summary for the score.
- **Extending horizons.** A larger step budget changes the target50 × 50 protocol. Put timing experiments in a separate profile
    and report their scores separately.
- **Actual batches.** The configured limit of eight yielded a mean size of 2.33.
    Inspect `batch-metrics` before tuning.
- **Randomness and scheduling.** `official_stream` depends on request order, batch size, and replica count. Hold resources
    and scheduling fixed for score comparisons, or identify the alternative RNG mode used in performance tests.
- **Dependency isolation.** Install RoboCasa and MuJoCo separately from the Transformers and PyTorch stack.
    Connect the environments through `--model-python` and `--sim-python`.
- **Video.** The reported time excludes video. Full recording adds encoding and disk I/O.
    Record selected failed tasks when analyzing behavior.
