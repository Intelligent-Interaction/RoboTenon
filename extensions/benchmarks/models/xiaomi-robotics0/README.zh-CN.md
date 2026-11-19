# Xiaomi Robotics 0

[English](README.md) · [**简体中文**](README.zh-CN.md)

该后端通过 RoboTenon 统一评测入口，在 LIBERO 上评测官方发布的
[`XiaomiRobotics/Xiaomi-Robotics-0-LIBERO`](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-0-LIBERO)
模型权重。

它复用上游评测器的双相机提示词、32 维
状态布局、逐观测稳定随机种子、连续 7 自由度动作，
以及每 10 步重新规划的配置。它支持将独立仿真器的请求
合批处理，同时保留每次观测对应的官方随机噪声。

将 checkpoint 下载到
`configs/libero.json` 指定的 `model_path`。在模型环境安装 `requirements.txt`，
并将本目录指定为运行器的模型插件。

## 全量运行前

- 模型环境与每个仿真环境应相互独立。基础
  LIBERO、LIBERO-Plus 与 LIBERO-Pro 使用相互重叠的包名，
  但对应不同的仿真源码与资产。
- 先用 batch size 1 运行一个 episode。`completed` 表示执行结束，
  实际任务成功与否需查看 `task_success`、`reason`、图像维度以及
  动作数值。
- 合批适配器需要 256×256 的 `agentview` 和 `wrist` 图像，以及
  8 维 LIBERO 状态。缺少腕部图像或误用离散化夹爪动作，
  均属于接口错误，应单独排查。
- `replan_steps=10`、连续 7 自由度动作与逐观测噪声种子
  属于模型接口。变更其中任何一项都会产生不同的运行条件。
- 本机 checkpoint 路径、账号、主机名和运行目录应保留在本地。
  将 JSON 复制到受忽略的本地目录修改，公开文档
  使用可移植的占位路径。

运行示例、实测结果与故障排查见以下指南。

- [基础 LIBERO](../../../../docs/tutorials/benchmark-h100-xiaomi-libero.zh-CN.md)
- [LIBERO-Plus](../../../../docs/tutorials/benchmark-h100-xiaomi-libero-plus.zh-CN.md)
