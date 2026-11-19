# LeRobot PI0.5（LIBERO）

[English](README.md) · [**简体中文**](README.zh-CN.md)

该后端加载 `lerobot/pi05_libero_base`，使用 checkpoint 自带的预处理、归一化统计和后处理，支持 RoboTenon 动态 batch。模型输入为两路 256×256 RGB 图像、8 维末端状态和任务文本；每次生成 50 步，按 checkpoint 配置执行前 10 步后重规划。

checkpoint 的 tokenizer 配置指向 gated 的 `google/paligemma-3b-pt-224`。已有 Hugging Face 访问权限时可直接使用；否则先把兼容 tokenizer 下载到远程目录，并通过 `tokenizer_path` 显式指定。不要在模型运行时从本地机器转发下载流量。

模型与依赖应在计算服务器上下载。示例路径见 `configs/libero.json`。安装 LeRobot 后，使用独立模型后端：

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

正式评测前先完成单任务 smoke，确认两路图像方向、状态维度、连续夹爪动作和 GPU 渲染均正常。

## LeRobot 独立基线

安装 `lerobot[pi,libero]` 后，可以绕过 RoboTenon，使用 LeRobot 自带的评测入口运行同一模型。该结果适合作为接入一致性基线：

```bash
CUDA_VISIBLE_DEVICES=0 MUJOCO_GL=egl lerobot-eval \
  --policy.path=/path/to/resources/models/pi05_libero_base \
  --policy.device=cuda \
  --env.type=libero --env.task=libero_object \
  --eval.batch_size=1 --eval.n_episodes=1 \
  --output_dir=/path/to/resources/runs/lerobot-smoke
```

扩大评测时同步调整 `eval.batch_size` 与 `eval.n_episodes`。与 RoboTenon 对比时应保持 task suite、episode 初始状态、horizon、模型随机种子和 action chunk 执行长度一致。
