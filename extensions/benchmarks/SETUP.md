# Set up and verify a benchmark

[**English**](SETUP.md) · [简体中文](SETUP.zh-CN.md)

See the [evaluation reference](EVALUATION.md) for multi-GPU runs, dynamic batching, scheduling, real-time mode, failure handling, and result aggregation.


## Choose an isolated runtime

Use Linux for the GPU simulator stacks unless a benchmark explicitly documents CPU/macOS support. Do not install all simulator requirements in one environment: MuJoCo, SAPIEN, Isaac, NumPy and torch versions conflict. Each benchmark's SETUP.md identifies the required stack and assets. Source pins are in its requirements and provenance files.

On the machine that will run the simulators, define absolute paths (adjust these examples):

```bash
export ROBOTENON=/opt/RoboTenon
export ASSETS=/data/benchmark-assets
export SIM_PY=/opt/envs/selected-simulator/bin/python
export MODEL_PY=/opt/envs/selected-model/bin/python
mkdir -p "$ASSETS"
cd "$ROBOTENON"
```

Create the environments with the Python version specified in the benchmark guide; RoboTenon requires Python >=3.10. `SIM_PY` and `MODEL_PY` must exist before running the commands. The model environment also runs the coordinator. They may be identical for a simple fake test, but separate environments are recommended for real models.

Read the benchmark's build-order instructions first, especially PyRep, CALVIN and CuRobo. With `BENCHMARK` set to its catalog ID, install:

```bash
export BENCHMARK=libero
"$SIM_PY" -m pip install -r "extensions/benchmarks/$BENCHMARK/requirements.txt"
"$SIM_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support -e "extensions/benchmarks/$BENCHMARK"
"$MODEL_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support
"$SIM_PY" -m pip check
"$MODEL_PY" -m robotenon_benchmark_support.evaluate --help
```

Do not install model torch/transformers dependencies into the simulator environment. Do not inject another environment's site-packages into PYTHONPATH. System packages (compiler, graphics drivers, Vulkan ICD, X11, CUDA toolkit) are separate from pip requirements. Download large assets directly on the server and preserve caches on persistent storage. Training datasets, simulator assets and model checkpoints are different resources; each guide distinguishes them.

## Profiles and paths

Built-in profiles may contain original container paths such as `/app`, `/assets` and `/workspace`. Copy to `local-profiles/`, edit the copy and pass its absolute filename to `--profile`. JSON values do not expand shell variables or `~`. Paths must be visible in the simulator process/container. Keep the exact asset version, task subset and any changed seed/horizon in the experiment record.

The three configuration types remain independent:

- Profile: tasks, episodes, seed, simulator observations, horizon and success protocol.
- Backend JSON: arguments passed to model `create(**params)`, including checkpoint and RNG.
- CLI execution arguments: devices, workers, batch size, batching delay and scheduler.

## Verify the result

First run the per-benchmark fake-action command with one worker and one episode. Use a fresh output directory. Installation is not confirmed merely because the command starts or reports a zero success rate.

1. Inspect `protocol.json` and `episode-plan.json` for the intended tasks, original indices, episode seeds and total count.
2. Inspect `runs/evaluation/status.json`, `summary.json` and each `episode.json`: no infrastructure errors, expected step progression and a recorded final outcome.
3. Inspect recorded observation/image artifacts: correct dimensions, finite state, nonempty/nonblack frames. Compare reset and later frames for an action that should visibly move the robot. A no-op may correctly leave the image unchanged.
4. Check native success and termination separately. Fake actions commonly fail the task; they cannot validate policy accuracy or prove the success path reachable for every task.
5. For batching, repeat with multiple workers and a backend declaring `supports_batch=True`; inspect request/session association and complete episode accounting.

```bash
find runs -name episode.json -o -name summary.json -o -name status.json
```

Results include per-episode, per-task, benchmark-defined dimensions and overall success. Failed attempts are retained; use the final aggregate rather than counting all attempt files as separate episodes. Keep generated Graph/module config and protocol metadata with the results. A task subset or shortened horizon must not be labeled a full official score.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Import fails before reset | Correct simulator Python, pinned dependencies, `pip check`, native shared libraries |
| Missing XML/mesh/task/state | Complete assets, correct absolute root, archive nesting, Git LFS files downloaded rather than pointer stubs |
| EGL errors or wrong GPU | Worker device binding and driver visibility; do not override all workers with one global EGL device |
| Vulkan failure | SAPIEN-compatible ICD and container graphics capabilities; CUDA visibility alone is insufficient |
| RLBench black images | X11/GLX + Qt xcb; use Xvfb on headless hosts, not Qt offscreen |
| First episode slow | Asset downloads, shader/JAX compilation or model warmup; complete setup before scaling |
| Fake success rate zero | Expected for a dummy policy; check steps/images/errors rather than interpreting this as model accuracy |

## What has actually been verified

The existing [native parity report](../../docs/changes/native-action-parity-2026-09-28.md) covers selected tasks for 18 non-Isaac simulators. [Unified-entry verification](../../docs/changes/unified-benchmark-entry-2026-09-29.md) covers model/runtime migration; [Kinetix realtime verification](../../docs/changes/kinetix-live-unified-2026-09-29.md) covers the live Graph. These are bounded probes, not all-task accuracy runs or fresh installation certification.

BEHAVIOR-1K and RoboDojo still require compatible RTX hardware, authorized assets and real rendering validation. Unit/config tests do not close that gap. This documentation update does not stop, restart or hot-update an existing evaluation.
