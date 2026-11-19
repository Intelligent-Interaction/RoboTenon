# Xiaomi Robotics-1 backend for RoboCasa365 and VLABench

[**English**](README.md) · [简体中文](README.zh-CN.md)

For VLA-only evaluation, use this directory's `vla.py`. Qwen, a system configuration, and a hierarchical graph are unnecessary.
`implementation.py` selects the official protocol from the session's `benchmark_id`. `preprocessing.py`
handles the two image, state, and prompt formats. RoboCasa365 uses a history length/interval of 4/2 and replans after 16 steps.
VLABench uses the current frame, `vlabench_choice`, and executes five of ten predicted actions.

Install `requirements.txt` in the model environment, copy `config.json`, and set `model_path`
to the checkpoint for the intended benchmark. The two benchmarks use different fine-tuned weights.
Use the matching checkpoint when reporting either benchmark.

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./extensions/benchmarks/models/xiaomi/config.json \
  --model-python /envs/model/bin/python --sim-python /envs/sim/bin/python \
  --model-devices 0 --workers-per-device 16 --batch-size 8 --wait-ms 50 \
  --max-tasks 1 --episodes-per-task 1 --output runs/xiaomi-smoke
```

Remove task/episode overrides to run the full protocol. The subset above is a smoke test.
`official_stream` uses global RNG. `session_deterministic` isolates RNG by stable episode identity.
For multiple GPUs or multiple replicas per GPU, see [virtual GPUs](../../VIRTUAL_GPUS.md). See [RoboCasa365 setup](../../robocasa365/SETUP.md) for simulator assets and dependencies.

The legacy system tool forwards to this backend. Both entry points use the same model implementation.

For a VLABench smoke test, use the same backend and change the benchmark, profile, and checkpoint settings:

```bash
Tenon evaluate \
  --benchmark vlabench --profile eval \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./extensions/benchmarks/models/xiaomi/config.vlabench.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 --output runs/xiaomi-vlabench-smoke
```

The VLABench checkpoint must support the processor robot key `vlabench_choice`. The simulator sends
Ego, Base, and Left-Wrist views in official VLABench order and the end-effector state in wxyz format. The backend
defaults to position offset `[0.0, -0.4, 0.78]`, 480×480 images, and `/no_cot`. Override these settings with
`vlabench_position_offset`, `vlabench_image_size`, `vlabench_cot`,
`vlabench_gripper_threshold` in the backend JSON.
