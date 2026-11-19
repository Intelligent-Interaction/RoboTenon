# Quickstart: run your first evaluation

[**English**](QUICKSTART.md) · [简体中文](QUICKSTART.zh-CN.md)

[Benchmark catalog](README.md) · [Installation](SETUP.md) · [CLI and parallel execution reference](EVALUATION.md)

This guide runs a small evaluation using LIBERO-Plus. To use another benchmark, select its ID, profile, simulator environment, and fake-action configuration.

## 1. Install the simulation environment

Choose an environment from the [benchmark catalog](README.md#benchmark-catalog). Follow its setup guide and prepare the required assets. [LIBERO-Plus](libero_plus/SETUP.md) requires a separate Python 3.10 simulator environment, pinned source revisions, and the Plus assets.

Set the following absolute paths on the host that will run the evaluation:

```bash
export ROBOTENON=/opt/RoboTenon
export MODEL_PY=/opt/envs/model/bin/python
export SIM_PY=/opt/envs/libero-plus/bin/python
export ASSETS=/data/benchmark-assets
cd "$ROBOTENON"
```

Create the Python environments separately. Follow the [shared installation instructions](SETUP.md) to install the framework and shared runtime in both environments. Install the benchmark in the environment selected by `SIM_PY` and the model dependencies in the environment selected by `MODEL_PY`. Download assets on the evaluation host.

## 2. Verify the installation with fake actions

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_plus --profile spatial \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --backend-config ./extensions/benchmarks/models/fake/configs/libero_plus.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-gpu 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/libero-plus-fake-smoke
```

If a profile contains container-specific paths, copy its JSON, update the paths to absolute paths on the host, and pass the copy to `--profile`. Use a fresh output directory for each run.

Check that the process exits normally, episodes finish without errors, environment steps advance, and observation images render correctly. Fake actions commonly yield zero task success. A zero action can also produce identical frames. To verify that frames respond to motion, choose an action that moves the robot.

## 3. Connect a model

Implement `vla.py` in its own directory using the [model interface](models/README.md). Declare support for `libero_plus`. In the command above, change these arguments:

```text
--backend /data/models/my-model/vla.py
--backend-config /data/models/my-model/config.json
```

The model must handle images, state, action denormalization, and conversion to the robot's action space. The Xiaomi backend described here declares support for RoboCasa365 only. LIBERO-Plus needs a compatible backend.

Keep the task subset small and use a single worker to verify model inputs and outputs.

## 4. Increase concurrency

After verifying that the backend supports batched inference, use these execution settings:

```text
--model-devices 0,1
--workers-per-gpu 4
--batch-size 4
--wait-ms 50
```

These settings start two model replicas and eight simulator workers. Each replica batches its own requests. The global queue distributes episodes, and each worker sends inference requests to its assigned GPU. See [parallel execution](EVALUATION.md#4-parallel-execution).

## 5. Run the full protocol and inspect the results

Select the intended profile from the benchmark README and remove the installation-check overrides for `--max-tasks` and `--episodes-per-task`. To evaluate all four LIBERO-Plus suites, run `all-0` through `all-3` separately.

| File | Contents |
| --- | --- |
| `protocol.json` | Effective protocol, model configuration, RNG, and execution settings |
| `episode-plan.json` | Episodes selected for the run |
| `runs/evaluation/status.json` | Live progress in the `summary` field |
| `runs/evaluation/summary.json` | Final results, which may be unavailable during execution |

See [outputs and progress](EVALUATION.md#10-outputs-and-progress) for success rates, errors, retries, and aggregation across tasks. The generated configuration records the full paths and command. Report smoke tests separately from full-protocol accuracy.
