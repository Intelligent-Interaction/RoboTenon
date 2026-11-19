# RoboTwin 2.0 full evaluation runbook

[**English**](RUNBOOK.md) · [简体中文](RUNBOOK.zh-CN.md)

This guide covers deployment, launch, monitoring, and troubleshooting for the full RoboTwin 2.0 evaluation. See the
[README](README.md) for protocols and basic use, [setup guide](SETUP.md) for installation and assets, and the
[evaluation reference](../EVALUATION.md) for shared framework options.

## 1. Protocol and performance settings

The complete official protocol covers:

- 50 tasks.
- Two settings, `demo_clean` (Easy) and `demo_randomized` (Hard).
- 100 episodes per task and setting.
- A total of `50 × 2 × 100 = 10,000` episodes.

[generate_official_profiles.py](scripts/generate_official_profiles.py) creates 100 profiles.
Each profile enumerates 100 episodes with `test_num=100`. Before a full run, verify that
`episode-plan.json` or `episodes.sqlite3` contains exactly 10,000 episodes.

Preserve the following protocol settings for comparable official results:

- Task selection and Easy/Hard configurations.
- Episode counts, seeds, and instruction types.
- Observations, action semantics, and success criteria.
- Task-specific horizons.

The adapter reads each task horizon from RoboTwin's
`env_cfg/task_config/_eval_step_limit.yml`. It falls back to 400 only if the file is missing or the value is invalid.
Overriding all tasks with one horizon changes the official protocol.

The following performance settings may be tuned independently. Record the values with the results:

- `workers-per-gpu`.
- `batch-size` and `wait-ms`.
- `startup-parallelism` and `enumeration-parallelism`.
- `recycle-after-tasks`.
- Scheduling strategy, timeouts, and thread environment variables.

## 2. Directory layout

Separate code, models, assets, and outputs. The following example uses shared storage:

```text
<shared-root>/
  models/pi0.5_robotwin2/
  profiles/robotwin2-official/
  sources/openpi/
  sources/robotwin-2.0/
  runs/
  logs/
```

The current adapter expects the RoboTwin checkout at `/app/RoboTwin`. If it lives on shared storage, use
a symlink and restore and verify it after rebuilding a node:

```bash
test -d "$SHARED_ROOT/sources/robotwin-2.0/assets"
ln -sfn "$SHARED_ROOT/sources/robotwin-2.0" /app/RoboTwin
test -f /app/RoboTwin/env_cfg/task_config/_eval_step_limit.yml
```

Full simulation requires RoboTwin source, embodiments, objects, background textures,
task configurations, and robot resources. A missing asset can show up only when a particular task starts. Training data serve a separate purpose.

## 3. Environment and version compatibility

Models and simulation may share a Conda environment when OpenPI, PyTorch, JAX, SAPIEN/MPLib, and
RoboTwin dependencies are compatible. A shared environment simplifies setup, but a dependency upgrade can affect
both inference and simulation.

The following combination was validated on one server:

| Component | Version |
| --- | --- |
| Python | 3.12 |
| PyTorch | 2.7.1 + CUDA 12.8 |
| NumPy | 1.26.4 |
| Transformers | 4.53.2 |
| Tokenizers | 0.21.4 |
| JAX | 0.7.2 |
| Orbax Checkpoint | 0.11.25 |
| SAPIEN | 3.0.0b1 |
| MPLib | 0.2.1 |
| AugMax | 0.3.4 |
| Beartype | 0.19.0 |

This table records a known working combination. Other hosts may require different versions. In particular:

- A top-level `import openpi` does not load all model dependencies. At minimum verify
  `from openpi.models import model`.
- Missing `augmax` or `beartype` may only surface during deeper model imports.
- Check OpenPI's dependency changes before downgrading an existing environment. Match JAX and Orbax. For example,
  JAX 0.7.2 with an older Orbax can fail on `jax.experimental.layout.DeviceLocalLayout`.
- Rerun the smoke test after changing NumPy, Transformers, JAX, Orbax, Pydantic, or Numpydantic.
  Previous environment results apply only to their recorded versions.
- Download dependencies and models on the server. Use a proxy only for connectivity and avoid forwarding large downloads through a local machine.

Check model imports before launch:

```bash
export PYTHONPATH="$SHARED_ROOT/sources/openpi/src${PYTHONPATH:+:$PYTHONPATH}"
"$MODEL_PY" - <<'PY'
import torch
import transformers
import jax
import orbax.checkpoint
from openpi.models import model

print("torch", torch.__version__)
print("transformers", transformers.__version__)
print("jax", jax.__version__)
print("openpi model import: OK")
PY
```

## 4. GPU rendering and NVIDIA graphics libraries

CUDA visibility in PyTorch and normal `nvidia-smi` output do not verify SAPIEN rendering. The container also needs
NVIDIA EGL/GLX/Vulkan user-space libraries compatible with the driver and the `graphics` capability.

Check the following:

```bash
nvidia-smi
ldconfig -p | grep -E 'libEGL_nvidia|libGLX_nvidia|libvulkan'
"$SIM_PY" - <<'PY'
import sapien
print(sapien.render.get_device_summary())
PY
```

The device summary should include all intended GPUs with `Supported` true. If it lists only a CPU renderer, adding
workers will not enable GPU rendering. Install graphics libraries matching the host driver.

## 5. Three preflight runs

Verify the following before scheduling 10,000 episodes:

1. **One worker with a fake backend.** Check assets, renderer, observations, and environment steps.
2. **One GPU, real model, one episode.** Check the checkpoint, tokenizer, three image streams, joint state,
   14-dimensional actions, and deep model dependencies.
3. **One GPU with several workers.** Check dynamic batching, session isolation, VRAM growth, and worker recycling.

Launch the eight-GPU run after all three checks finish without infrastructure errors. A fake policy may score zero.
Confirm that the environment advances, images are valid, and episodes are error-free.

## 6. Full-run configuration

The repository's [run_official_full.sh](scripts/run_official_full.sh) launches the full configuration:

```bash
export REPO_ROOT=/path/to/RoboTenon
export SHARED_ROOT=/data/robotwin
export MODEL_PY=/path/to/conda/env/bin/python
export SIM_PY="$MODEL_PY"
export OUTPUT="$SHARED_ROOT/runs/pi05-robotwin2-$(date +%Y%m%d-%H%M%S)"

bash "$REPO_ROOT/extensions/benchmarks/robotwin/scripts/run_official_full.sh"
```

Its baseline performance settings are:

| Setting | Value |
| --- | ---: |
| GPU | 8 |
| Worker / GPU | 12 |
| Total workers | 96 |
| Maximum batch size | 32 |
| `wait-ms` | 50 |
| `startup-parallelism` | 12 |
| `enumeration-parallelism` | 24 |
| `recycle-after-tasks` | 8 |
| Scheduling | `global_queue` |
| Trace | `summary` |

Model and tokenizer paths are in
[configs/robotwin2.json](../models/pi05-robotwin2/configs/robotwin2.json). JSON does not expand environment variables.
Set valid absolute paths on the target host.

Launch full runs through `tmux`, a job scheduler, or `setsid`, with stdout/stderr written to a separate log. Use
a fresh output directory for each run. Treat a restart as a separate run unless results are explicitly reconciled.

## 7. Threads, workers, and memory

With many workers, bound common numerical library thread pools:

```bash
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false
```

These variables reduce thread contention. SAPIEN, CUDA, file I/O, and the Python
runtime can still create helper threads.

When selecting workers per GPU, consider more than CPU core counts. A RoboTwin worker can allocate several GiB
after scene loading and additional GPU memory through the renderer/driver. Excess concurrency previously caused a node to become unreachable. The conservative baseline uses 12 workers per GPU.
Increase concurrency in small steps while monitoring:

- Steady-state episodes/min after startup.
- cgroup `memory.current`, `memory.max`, and `memory.events`.
- Worker RSS, GPU memory, and whether allocations fall after renderer reset.
- Processes in `D` state and shared-storage read latency.
- Time to recycle and reinitialize a worker.

`startup-parallelism=12` limits concurrent cold starts. Early CPU and memory utilization may be low and fewer than 96 workers may be visible
as workers start in waves. Increasing startup parallelism reduces cold-start time but adds simultaneous pressure on shared storage, memory,
and the graphics driver.

`recycle-after-tasks=8` limits long-lived simulator resource accumulation. Recycling too frequently rebuilds scenes and reduces throughput,
while a much larger interval can allow memory or renderer state to accumulate.

## 8. Startup phases

A full run typically proceeds through these stages:

1. Generate 100 profiles.
2. Start eight model replicas and load checkpoints.
3. Enumerate 10,000 episodes with 24-way parallelism.
4. Reduce enumeration results into the global task queue.
5. Cold-start up to 96 simulator workers in waves.
6. Enter steady simulation and dynamic batching.
7. Aggregate results, write the summary, and close services.

The first five stages can take several minutes. During this time:

- GPUs may hold model weights while showing utilization near 0%.
- CPUs may be mostly idle.
- The episode database may first be empty, then show 10,000 pending episodes at once.
- Worker counts may grow slowly.
- Several processes may be in `D` state waiting for shared storage, scene assets, or driver initialization.

To distinguish a stall from startup, check child process turnover, database update times,
`status.json`, GPU memory, logs, and output timestamps over time rather than drawing a conclusion from a single GPU utilization sample.

## 9. Progress, throughput, and success

Read the following files during a run:

```text
<output>/runs/evaluation/status.json
<output>/runs/evaluation/episodes.sqlite3
<output>/batch-metrics/*.json
```

`completed` means an episode executed normally. Actual task success is recorded in `task_success`. An episode that reaches its horizon
with `task_success=false` is a policy failure. With small samples, report both the numerator and denominator,
for example `0/2`.

Read current progress from the framework status file:

```bash
export EVAL_OUTPUT=/data/robotwin/runs/example
export EVAL_RUN_ID=evaluation
"$MODEL_PY" - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["EVAL_OUTPUT"]) / "runs" / os.environ["EVAL_RUN_ID"] / "status.json"
status = json.loads(path.read_text())
summary = status.get("summary", {})
print({
    "status": status.get("status"),
    "total": status.get("total_episodes"),
    "completed": status.get("completed_episodes"),
    "active": len(status.get("active", [])),
    "successes": summary.get("successes"),
    "success_rate": summary.get("episode_success_rate"),
    "errors": summary.get("num_errors"),
    "cancelled": summary.get("num_cancelled"),
})
PY
```

Use distinct throughput measures:

- **End-to-end throughput:** completed episodes divided by time since main process launch, including model loading and cold start.
- **Steady-state throughput:** episodes completed during the last 5–10 minutes divided by that interval. This is suitable for estimating remaining time.
- **Per-episode simulation speed:** environment steps divided by episode execution time. Track this to detect simulator slowdown.

The first episodes often run to the full horizon. Early in the run, 96 workers may be active with few completed episodes.
Wait for at least several dozen completions before estimating ETA and success rate.

`batch-size=32` sets an upper bound. Read the observed distribution from `batch-metrics/*.json`. With asynchronous arrivals,
mean batch size may remain far below 32 even with 96 workers.

## 10. Troubleshooting

### Model service exits immediately after startup

Run the deep OpenPI import check with the same interpreter, then verify missing packages and JAX/Orbax compatibility. A top-level import alone does not establish
that the checkpoint can load.

### All eight GPUs hold memory but show 0% utilization

Check whether episode enumeration, reduction, or worker cold start is still in progress. Once episodes show as running,
check for observation requests from workers, increasing batch metrics, and a GPU renderer detected by SAPIEN.

### CPU and memory utilization are low

Check worker counts and process states. Workers may still be launching, or many processes may be waiting in `D` state for assets
and driver initialization. Increasing `OMP_NUM_THREADS` will not resolve these waits.

### CPU usage is high without throughput gains

Check BLAS/OpenMP thread limits, worker counts, shared-storage reset latency, and whether actual batches are
concentrated at size 1. Fewer workers can improve throughput when concurrency is excessive.

### Memory keeps growing or the node disconnects

Reduce worker counts, retain `recycle-after-tasks`, and check cgroup limits in addition to host `free` output. Without swap,
a cgroup OOM or failed platform health check may disconnect the server. After recovery, verify local environments, symlinks, and any uncommitted
source changes before restarting.

### Low task success

Distinguish unsuccessful tasks, episodes ending at the horizon, and infrastructure errors. Investigate adapters and environments when infrastructure errors appear.
Preserve the protocol horizon, all failed episodes, and ordinary policy failures in the reported score.

## 11. Recovery after rebuilding a node

Restore and verify dependencies before restarting:

1. Restore the complete RoboTenon working tree, including any required benchmark changes.
2. Restore `/app/RoboTwin` with its complete simulator source and assets.
3. Verify the checkpoint, tokenizer, OpenPI source, and free space on output storage.
4. Restore the Conda environment and editable installations.
5. Verify NVIDIA EGL/GLX/Vulkan and SAPIEN GPU devices.
6. Run the deep OpenPI import check.
7. Run a one-episode smoke test with the real model.
8. Start the full evaluation and confirm the main process remains alive.
9. Confirm eight model replicas load, all 10,000 episodes enter the queue, and 96 workers start in waves.
10. Wait for completions before reporting throughput, success rate, or ETA.

Verify stages 8–9 and process liveness before reporting a successful full-run launch. A returned command or generated profiles
only confirms an earlier stage.

## 12. Final report

After completion, record:

- Source, RoboTwin, OpenPI, and checkpoint revisions.
- Task coverage, total episodes, seeds, and horizon source.
- GPUs, CPUs, memory, and model/simulator environment versions.
- Workers, batch limits, wait windows, recycling, scheduling, and timeouts.
- Total runtime and end-to-end and steady-state throughput.
- Counts of completed, successful, unsuccessful, errored, and cancelled episodes.
- Easy, Hard, per-task, and overall success rates.
- Observed batch-size distribution.
- Video, step tracing, and other recorders affecting performance.
- Known deviations and categorized failure causes.

Report full results only after accounting for `10,000/10,000` episodes and explicitly reporting errors/cancellations. Interim success rates,
particularly from the first few completions, cannot establish the final score.
