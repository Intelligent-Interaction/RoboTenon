# LeRobot PI0.5（LIBERO）

[**English**](README.md) · [简体中文](README.zh-CN.md)

The backend loads `lerobot/pi05_libero_base` and uses the checkpoint's preprocessing, normalization statistics, and postprocessing with dynamic batching. Inputs are two 256×256 RGB images, an eight-dimensional end-effector state, and task text. Each prediction returns 50 steps. The checkpoint configuration executes the first ten before replanning.

The checkpoint tokenizer configuration references the gated `google/paligemma-3b-pt-224`. Use authorized Hugging Face access or download a compatible tokenizer on the evaluation server and specify `tokenizer_path`. Download directly on the server before inference.

Download model files and dependencies on the compute server. See `configs/libero.json` for example paths. After installing LeRobot, select the standalone backend:

```bash
Tenon evaluate \
  --benchmark libero --profile object \
  --backend ./extensions/benchmarks/models/pi05/vla.py \
  --backend-config ./extensions/benchmarks/models/pi05/configs/libero.json \
  --model-python /path/to/resources/envs/pi05-model/bin/python \
  --sim-python /path/to/resources/envs/pi05-model/bin/python \
  --model-devices 0 --workers-per-gpu 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 \
  --output /path/to/resources/runs/smoke
```

Run a single-task smoke test before full evaluation. Check both camera orientations, state dimensions, continuous gripper actions, and GPU rendering.

## Standalone LeRobot baseline

After installing `lerobot[pi,libero]`, run the same model with LeRobot's own evaluator to obtain an independent integration baseline:

```bash
CUDA_VISIBLE_DEVICES=0 MUJOCO_GL=egl lerobot-eval \
  --policy.path=/path/to/resources/models/pi05_libero_base \
  --policy.device=cuda \
  --env.type=libero --env.task=libero_object \
  --eval.batch_size=1 --eval.n_episodes=1 \
  --output_dir=/path/to/resources/runs/lerobot-smoke
```

Scale `eval.batch_size` and `eval.n_episodes` together. Align the suite, initial states, horizon, model seed, and executed action-chunk length when comparing with RoboTenon.
