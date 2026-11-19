# RoboCasa365：协议与实现参考

[English](NATIVE_EVALUATION.md) · [**简体中文**](NATIVE_EVALUATION.zh-CN.md)

此页保留旧链接。用户使用说明已集中到以下入口，避免多份命令发生偏差：

- [任务协议、profile 与启动命令](README.zh-CN.md)
- [环境安装与资产下载](SETUP.zh-CN.md)
- [统一评测与并行说明](../EVALUATION.zh-CN.md)
- [独立模型接口](../models/README.zh-CN.md)

环境协议实现在 `src/robotenon_robocasa365/benchmark.py`；模型实现不属于环境协议。`benchmark-source.json` 记录迁移来源，不能据此推断所有仿真任务或最终成功率已与上游完全一致。

## Xiaomi 一致性检查

与 Xiaomi 原版评测器对比时，使用一致的任务索引、episode 种子、权重和任务 horizon。
单样本参考配置为每个模型服务对应一个仿真器。
动态合批需要另行比较成功率，仅有一段 action chunk 相符
不能证明完整 rollout 一致。

加载器按 Xiaomi 服务端的实现，在加载后将整个模型显式转换为 BF16。
仅设置 Hugging Face 加载时的 dtype 会使 RoPE 频率缓冲区保持 FP32。
协议测试通过包含 FP32 缓冲区及
BF16 权重的模型检查这项差异。

扩展还保护 robosuite 的 EGL 渲染器清理流程。robosuite 1.5.2 可能在另一渲染器仍活动时，
回收前一次 reset 遗留的渲染器。其析构函数可能在未绑定所属 GL 上下文时
释放 GPU 资源，导致当前渲染器图像损坏，
而物理仿真仍正常推进。
`_internal/rendering.py` 在清理期间绑定旧渲染器所属上下文，
之后恢复原先活动的上下文。这只改变资源清理行为，
物理、相机和 episode 种子保持一致。修复仅对 EGL Worker 生效。

以下仿真回归测试可独立于模型推理运行。

```bash
MUJOCO_GL=egl python extensions/benchmarks/robocasa365/tools/check_egl_cleanup.py \
  --output runs/robocasa365-egl-check
```

脚本启动三个独立进程，分别作为 rollout 中途不做 GC 的参考组、
强制 GC 且无保护组，以及强制 GC 且启用保护组。脚本保存观测，
验证受保护组的机器人状态完全相同，RGB 像素的最大差值
不超过 1。允许一级像素差，是因为重复运行参考组时，
光栅化本身也会出现细微差异。

评测期间从 `status.json` 或 `episodes.sqlite3` 读取实时进度。
首个 episode 完成前就可能创建初始 `summary.json`，
因此仅凭该文件存在不能判断运行已完成。
