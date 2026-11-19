# Benchmark guide

[**English**](benchmarks.md) · [简体中文](benchmarks.zh-CN.md)

Use one CLI to select the simulator, evaluation protocol, and model, then inspect success rates. Run the commands from the repository root. Replace example paths under `/opt` and `/data` with local paths.

No prior Graph experience is required. The evaluator generates the Graph, Module, Tool, and simulator configurations. To evaluate a planner, VLA, and verifier together, use a [system configuration](../extensions/benchmarks/SYSTEMS.md) with the same entry point.

For multi-GPU examples, see [Xiaomi + RoboCasa365](tutorials/benchmark-h100-xiaomi.md),
[Xiaomi + VLABench](tutorials/benchmark-h100-xiaomi-vlabench.md),
[Xiaomi Robotics 0 + LIBERO](tutorials/benchmark-h100-xiaomi-libero.md)、
and [Xiaomi Robotics 0 + LIBERO-Plus](tutorials/benchmark-h100-xiaomi-libero-plus.md). These guides cover hardware capacity, parallel settings, model configuration, measured results, and troubleshooting.

## 1. Select an environment, protocol, and model

| Choice | Option | Meaning |
| --- | --- | --- |
| Environment | `--benchmark robocasa365` | Simulator stack and action/observation representation |
| Protocol | `--profile xiaomi_official` | Task set, episode count, seed, horizon, and observation specification. Also accepts a JSON path |
| Model | `--backend /data/models/my-model/vla.py` | Standalone model factory. A reference such as `my_model.vla:create` also works |
| Model configuration | `--backend-config /data/models/my-model/config.json` | Checkpoint, precision, RNG, and other parameters passed directly to `create(**params)` |
| Execution resources | GPU, Worker, and batch options | Configure execution independently of the evaluation protocol |

The [benchmark index](../extensions/benchmarks/README.md#benchmark-catalog) lists all 20 environments with their README, SETUP, and profiles. Each SETUP documents installation, assets, licensing, and validation status. BEHAVIOR-1K and RoboDojo require their respective Isaac/RTX stacks. Actual rendering on those systems has yet to be verified.

Each backend must explicitly support the selected benchmark. Changing a benchmark name cannot adapt a model to another robot. `models/xiaomi` targets RoboCasa365. `models/xiaomi-robotics0` targets LIBERO, LIBERO-Plus, and LIBERO-Pro. The four LIBERO-Plus suites, `all-0` through `all-3`, can be scheduled together with `--profiles all-0 all-1 all-2 all-3`. See [LIBERO-Plus](../extensions/benchmarks/libero_plus/README.md).

## 2. Install dependencies and download assets

Use separate Python environments for model inference and simulation. Isolate different benchmarks' simulator dependencies to avoid conflicts between MuJoCo, SAPIEN, Isaac, NumPy, and torch versions. Both Python environments should run on the same server with access to the same files.

```bash
export MODEL_PY=/opt/envs/model/bin/python
export SIM_PY=/opt/envs/robocasa365/bin/python
export BENCHMARK=robocasa365

"$MODEL_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support
# Follow the chosen benchmark's SETUP for system libraries, simulator dependencies, and build order before installing its adapter.
"$SIM_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support \
  -e "extensions/benchmarks/$BENCHMARK"
"$MODEL_PY" -m robotenon_benchmark_support.evaluate --help
```

These commands install code. Download simulator assets and model weights on the execution server as described in [shared setup](../extensions/benchmarks/SETUP.md) and the environment's SETUP. Keep model packages in the model environment. Injecting another environment's `site-packages` can introduce conflicts. Use absolute asset paths in JSON, which does not expand `$VARIABLE` or `~`.

## 3. Validate one episode

With RoboCasa365 installed, run the following command.

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile smoke \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --backend-config ./extensions/benchmarks/models/fake/configs/robocasa365.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/robocasa365-fake-smoke
```

A Fake policy still runs the real simulator and therefore needs assets and rendering support. Check that the episode has no errors, steps advance, and state and images are valid. A zero action may leave the scene unchanged. To verify motion, use a nonzero action supported by that environment. A zero success rate with the Fake policy can be expected.

Set `--backend` and `--backend-config` for the real model. Keep one Worker and a small task subset while checking input and output. Implement `SPEC`, `create()`, and the session/batch interface in a standalone `vla.py` for a custom model. The evaluator, Graph, and benchmark need no changes. See the [model interface](../extensions/benchmarks/models/README.md).

## 4. Scale to multiple GPUs and the full protocol

The following example runs Xiaomi on RoboCasa365's full target50 × 50 protocol. Install the [Xiaomi backend dependencies](../extensions/benchmarks/models/xiaomi/README.md), copy its configuration to `/data/configs/xiaomi.json`, set `model_path` to the absolute path of the downloaded checkpoint, and choose `rng_mode` explicitly.

```bash
# Limit numerical library threads in each child process to reduce oversubscription across Workers.
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config /data/configs/xiaomi.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 --workers-per-device 16 \
  --batch-size 8 --wait-ms 50 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --startup-parallelism 16 --recycle-after-tasks 5 \
  --output runs/robocasa365-xiaomi-full
```

This starts **8 model replicas and 128 simulation Workers**. Using 28 Workers per GPU gives 224 Workers; 24 per GPU gives 192. Adjust concurrency for the machine's memory, CPU, VRAM, and rendering capacity. The environment variables above limit common library thread pools. They do not cap the total number of threads in each process.

For a full run, remove task or episode overrides used for smoke tests, and choose a fresh output directory. The full RoboCasa365 profile preserves each task's original horizon and checks for success after every step, ending the episode immediately on success. Other environments keep their own protocol semantics.

| Feature | Usage and limits |
| --- | --- |
| Dynamic batching | `--batch-size 8 --wait-ms 50` caps the batch at 8. Actual batches may contain 1–8 requests. The 50 ms setting excludes additional queueing and inference latency |
| Multiple GPUs | Each device hosts a resident model service. Workers stay bound to their service, and each service batches independently |
| Multiple replicas on one GPU | Configure logical slots using `--virtual-gpu-map`. Additional replicas consume VRAM and share hardware. See [virtual GPUs](../extensions/benchmarks/VIRTUAL_GPUS.md) |
| Global episode queue | The default shuffles the queue. Idle Workers claim episodes dynamically. Inference requests are not pooled across GPUs, and active episodes stay on their original Workers |
| Static scheduling | Use `--scheduling-strategy static --no-shuffle` for comparison |
| Startup/reset concurrency | `--startup-parallelism` limits simultaneous initializations independently of total Worker count |
| Worker recycling | `--recycle-after-tasks 5` rebuilds each Worker after five executions. The count applies to executions, not complete sets of episodes for five task types. Model services remain resident |
| Live environments | Kinetix `realtime` selects a live Graph. Environment and model advance asynchronously. See [Kinetix](../extensions/benchmarks/kinetix/README.md) |
| Complete systems | `--system-config` selects the Graph; `--execution-config` configures the model services. See [system evaluation](../extensions/benchmarks/SYSTEMS.md) |

Store execution options in the JSON specified by `--execution-config`. Explicit CLI flags override defaults from the file. See the [evaluation manual](../extensions/benchmarks/EVALUATION.md#8-cli-options) for all options and EGL binding.

Shuffling uses an independent scheduling RNG and preserves original task indices, episode numbers, and environment seeds. **Model trajectories and final success rates can still depend on scheduling.** Xiaomi's `official_stream` uses a service-wide RNG. Request order, replica count, and batching can change which random draws each request gets. Use `session_deterministic` for performance comparisons with per-session RNG. Floating-point differences between batches can remain.

For the full RoboCasa365 protocol, eight H100s and 224 Workers completed 2,500 episodes in **106.70 minutes**, at **23.43 episodes/min** end to end with **57.56%** success. See [evaluation results and parallel efficiency](tutorials/benchmark-h100-xiaomi.md#results-and-parallel-efficiency) for details and public data.

On LIBERO, eight H100s and 288 Workers completed 2,000 episodes in **20.59 minutes**, at **97.15 episodes/min** with **97.15%** success. See [Xiaomi Robotics 0 + LIBERO](tutorials/benchmark-h100-xiaomi-libero.md) for failures by task and reproduction notes.

On LIBERO-Plus, eight H100s and 288 Workers completed 10,030 perturbation cases in **127.95 minutes**, at **78.39 episodes/min** with **74.77%** success. See the [full Xiaomi Robotics 0 + LIBERO-Plus evaluation](tutorials/benchmark-h100-xiaomi-libero-plus.md).

## 5. Inspect progress, speed, and results

| File relative to the output directory | Contents |
| --- | --- |
| `protocol.json` | Original and effective protocols, overrides, backend, RNG, scheduling, and execution settings |
| `episode-plan.json` | Effective plan and stable episode identities |
| `config*.json` | Generated application, Graph, and Module configurations |
| `runs/evaluation/status.json` | In-progress summary and active state |
| `runs/evaluation/summary.json` | Final overall, per-task, and per-dimension statistics |
| `runs/evaluation/episodes.sqlite3` | Episode index for completed counts and recent throughput |
| `batch-metrics/*.json` | Per-service batch size distribution, request counts, queue time, and inference time |

If you change `--run-id`, replace `evaluation` in these paths. During a run, summary.json can lag. Query status.json or the episode index. The following read-only command counts final episode records without double-counting attempts.

```bash
export EVAL_OUTPUT=/data/runs/robocasa365-xiaomi-full
"$MODEL_PY" - <<'PY'
import datetime as dt
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

path = Path(os.environ['EVAL_OUTPUT']) / 'runs/evaluation/episodes.sqlite3'
with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
    rows = db.execute('SELECT updated_at, record_json FROM episodes').fetchall()
records = [(dt.datetime.fromisoformat(t.replace('Z', '+00:00')), json.loads(r))
           for t, r in rows]
counts = Counter(r.get('status', 'unknown') for _, r in records)
done = [(t, r) for t, r in records if r.get('status') == 'completed']
successes = sum(bool(r.get('task_success')) for _, r in done)
now = dt.datetime.now(dt.timezone.utc)
rates = {m: sum(0 <= (now-t).total_seconds() <= m*60 for t, _ in done) / m
         for m in (5, 10, 30)}
print('Status:', dict(counts), 'planned records:', len(records))
print('Successes:', successes, 'success rate among completed episodes:', successes / len(done) if done else None)
print('Recent episodes/min:', rates)
PY
```

When successful episodes terminate early and failed episodes run to their horizon, the success rate among the first completions tends to be high even with a shuffled queue. Use the full plan for final aggregation. Preserve errors and cancellations separately so infrastructure failures remain visible.

For an ETA, divide the remaining episode count by throughput over the last 10–30 minutes. Cold starts, environment rebuilds, and long final episodes affect the estimate. To obtain the mean batch size, divide summed `requests` by summed `batches` across services. Use differences between two snapshots for a recent interval. Interpret instantaneous GPU utilization alongside completion rate, errors, memory, and queues.

## 6. Interruptions, performance, and troubleshooting

The unified entry point has no automatic `--resume`. A WorkerLost retry within a run differs from resuming the whole evaluation. Keep the original directory after an interruption, enumerate the episodes to rerun, and specify a deduplication rule. Use a fresh output directory.

Run server jobs in a persistent session such as tmux. Validate one Worker and one GPU before increasing concurrency. If batches remain small, inspect each service's request rate. More Workers or a longer wait window may not improve throughput. The defaults are `trace-mode=summary` and `history-cache=auto`. Diagnostic windows and model observation history are independent. Xiaomi's 4/2/16 history settings produce no overlap between adjacent requests, so auto leaves incremental caching disabled.

See [execution options and failure handling](../extensions/benchmarks/EVALUATION.md), the [model interface](../extensions/benchmarks/models/README.md), [Graph lifecycle](native-benchmark-graph.md), and [setup troubleshooting](../extensions/benchmarks/SETUP.md#troubleshooting).
