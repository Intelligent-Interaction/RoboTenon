# Benchmark 参考运行

[English](reference-runs.md) · [**简体中文**](reference-runs.zh-CN.md)

以下两次 Xiaomi-Robotics-1 评测各使用 8 张 NVIDIA H100，执行错误与取消次数均为零。各评测报告提供了启动命令、环境设置、逐任务结果和计时方法。

| Benchmark | 仿真 Worker 数 | 完成的 episode 数 | 任务宏平均成功率 | 总耗时 | 端到端吞吐 |
| --- | ---: | ---: | ---: | --- | ---: |
| [RoboCasa365](tutorials/benchmark-h100-xiaomi.zh-CN.md) | 224 | 2,500 | 57.56% | 1 小时 46 分 42 秒 | 23.43 episodes/分钟 |
| [VLABench](tutorials/benchmark-h100-xiaomi-vlabench.zh-CN.md) | 160 | 2,460 | 60.84% | 约 1 小时 54 分钟 | 21.58 episodes/分钟 |

任务宏平均赋予每个任务相同的权重。RoboCasa365 每项任务的 episode 数量相同，因此其宏平均成功率等于按 episode 加权的成功率。VLABench 按任务平均为 60.84%，按 episode 加权为 61.18%。各 benchmark 有各自的任务与步数预算。比较不同策略时应使用相同协议。

其他模型与环境组合见[教程目录](tutorials/README.zh-CN.md)。[Benchmark 使用指南](benchmarks.zh-CN.md)介绍了仿真资产、模型依赖，以及如何从单 Worker 冒烟验证扩展到完整评测。
