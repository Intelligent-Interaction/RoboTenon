# OpenPI pi0.5（RoboTwin 2.0）

[English](README.md) · [**简体中文**](README.zh-CN.md)

该后端加载 `motus-robotics/pi0.5_robotwin2`，使用 checkpoint 的 joint-state
归一化统计、三路 RGB 图像和 14 维双臂动作。模型一次生成 32 步动作，默认执行前
8 步后重新规划。

后端包含旧版 OpenPI `resize_with_pad_torch` 的 batch=1 兼容处理，因此无需修改
OpenPI checkout。模型与 tokenizer 的示例路径位于
[`configs/robotwin2.json`](configs/robotwin2.json)。

官方完整口径为 50 个任务 × 每任务 100 rollouts × `demo_clean` 和
`demo_randomized` 两种设置，共 10,000 episodes。生成 profiles 并按 8 卡、每卡
12 个 worker、最大 batch 32 启动：

```bash
export SHARED_ROOT=/path/to/resources
export BACKEND_CONFIG=/path/to/local-model-config.json
bash extensions/benchmarks/robotwin/scripts/run_official_full.sh
```

运行前应先用单任务、单 episode 验证模型能返回动作并实际推进仿真。完整的环境检查、
资源配置、监控和故障恢复说明见 [`robotwin/RUNBOOK.md`](../../robotwin/RUNBOOK.zh-CN.md)。
