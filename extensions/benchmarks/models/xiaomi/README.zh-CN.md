# Xiaomi Robotics-1：RoboCasa365 / VLABench 双 benchmark 后端

[English](README.md) · [**简体中文**](README.zh-CN.md)

纯 VLA 评测使用本目录的 `vla.py`，不需要系统配置、Qwen 或分层 Graph。
`implementation.py` 按 session 的 `benchmark_id` 选择官方协议，`preprocessing.py`
负责两套图像、state 和 prompt 处理。RoboCasa365 使用 4/2 历史、16 步重规划；
VLABench 使用当前帧、`vlabench_choice`、10 步 action chunk 中执行 5 步。

在模型 Python 环境安装 `requirements.txt`，复制 `config.json` 并将 `model_path`
改为与目标 benchmark 匹配的 checkpoint。两个 benchmark 使用不同的微调权重，
不要用 RoboCasa365 checkpoint 报告 VLABench 成绩，反之亦然。

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./extensions/benchmarks/models/xiaomi/config.json \
  --model-python /envs/model/bin/python --sim-python /envs/sim/bin/python \
  --model-devices 0 --workers-per-device 16 --batch-size 8 --wait-ms 50 \
  --max-tasks 1 --episodes-per-task 1 --output runs/xiaomi-smoke
```

删除任务/episode 覆盖可运行完整协议；以上子集结果不能作为正式准确率。
`official_stream` 使用全局 RNG，`session_deterministic` 使用稳定 episode 身份隔离 RNG。
多卡和同卡多副本见 [虚拟 GPU](../../VIRTUAL_GPUS.zh-CN.md)。仿真依赖和资产见 [RoboCasa365 安装](../../robocasa365/SETUP.zh-CN.md)。

系统中的旧 Tool 入口仅转发到这里，两条路径使用同一份模型实现。

VLABench smoke 使用同一个后端，只替换 benchmark、profile 和 checkpoint 配置：

```bash
Tenon evaluate \
  --benchmark vlabench --profile eval \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./extensions/benchmarks/models/xiaomi/config.vlabench.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 --output runs/xiaomi-vlabench-smoke
```

VLABench checkpoint 必须支持 processor robot key `vlabench_choice`。仿真端按照
VLABench 官方相机顺序传输 Ego、Base、Left-Wrist 三视角和 wxyz 末端状态；模型端
使用默认位置偏移 `[0.0, -0.4, 0.78]`、480×480 图像和 `/no_cot`。这些值可通过
`vlabench_position_offset`、`vlabench_image_size`、`vlabench_cot`、
`vlabench_gripper_threshold` 在 backend JSON 中覆盖。
