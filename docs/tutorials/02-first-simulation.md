# First benchmark simulation

[**English**](02-first-simulation.md) · [简体中文](02-first-simulation.zh-CN.md)

Use the RoboCasa365 extension to check simulation steps, images, actions, and the terminal signal.

## 1. Install the extension

Run this command in the RoboCasa simulation environment:

```bash
python -m pip install -e . -e extensions/benchmarks/benchmark-support -e extensions/benchmarks/robocasa365
python -m pip install -r extensions/benchmarks/robocasa365/requirements.txt
```

Follow the extension README to install the RoboCasa365 assets. The standalone fake backend at
`extensions/benchmarks/models/fake/vla.py` lets you check that the simulator advances.

## 2. Run the smoke profile

```bash
Tenon evaluate --benchmark robocasa365 \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --backend-config ./extensions/benchmarks/models/fake/configs/robocasa365.json \
  --sim-python /path/to/robocasa/python \
  --profile extensions/benchmarks/robocasa365/configs/smoke.json \
  --output runs/robocasa365-smoke
```

The command generates `config.json`, `config_graphs.json`, and `config_modules.json`, then
runs `reset → policy → execute → observe → terminal`. In the episode JSON, inspect
`graph_status`, `outputs.env_steps`, and `outputs.native_metrics`, along with the recorded images. The fake
action tests the integration. Model accuracy requires a real policy.

## 3. Connect a model

Implement `vla.py` in a separate model directory. Declare `SPEC` and provide `create()`, `open_session`,
`act_batch`, `close_session`, and `close`. Pass the file path with `--backend`.
Let the environment handle reset, step, and success evaluation. Rerun the same command
with the new backend.

For the full Xiaomi-Robotics-1 target50 × 50 command, see
[extension README](../../extensions/benchmarks/robocasa365/README.md)。
