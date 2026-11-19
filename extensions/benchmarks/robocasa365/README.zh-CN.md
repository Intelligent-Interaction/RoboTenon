# RoboCasa365

[English](README.md) · [**简体中文**](README.zh-CN.md)

[全部 benchmark](../README.zh-CN.md) · [安装与资产](SETUP.zh-CN.md) · [快速开始](../QUICKSTART.zh-CN.md) · [并行与完整参数](../EVALUATION.zh-CN.md)

统一入口的 benchmark ID 为 **`robocasa365`**。该 extension 负责环境、任务、seed、horizon、观测/action 规格及成功判定；模型放在独立目录，通过 `--backend` 指定。

## 使用顺序

1. 按 [SETUP](SETUP.zh-CN.md) 安装固定版本的仿真环境，下载资产并设置路径。
2. 使用 SETUP 中的 fake-action 命令，确认环境可以推进、图像正常且无运行错误。
3. 按 [模型接口](../models/README.zh-CN.md) 接入模型，先做小任务子集验证。
4. 选择下面的 profile，按 [完整手册](../EVALUATION.zh-CN.md) 配置单卡或多卡评测。

## 选择评测协议

默认起点是 `smoke`。每个 JSON 中的 `params` 为环境设置，`episodes_per_task` 为每任务运行次数；表中“未显式设置”表示需要查看 benchmark 实现的默认行为。

| Profile | 任务集 / 模式 | 每任务 episodes |
| --- | --- | ---: |
| [`smoke`](configs/smoke.json) | pretrain | 1 |
| [`xiaomi_official`](configs/xiaomi_official.json) | pretrain | 50 |

`xiaomi_official` 为 target50 ×50、pretrain、seed 7、256像素观测、原始任务 horizon；`smoke` 仅用于连通验证。

`--profile` 支持上述名称或外部 JSON。需要改资产路径时复制配置并使用绝对路径；需要做小规模验证时使用 `--max-tasks` 和 `--episodes-per-task`。正式报告必须记录覆盖项，不能把子集标记成完整协议。

## 最小命令

完成安装后，在 RoboTenon 仓库根目录运行。`MODEL_PY`、`SIM_PY` 的设置见 [快速开始](../QUICKSTART.zh-CN.md)。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile smoke \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/robocasa365-model-smoke
```

## Xiaomi Robotics 1

设置 `../models/xiaomi/config.json` 中的 `model_path` 后运行下列命令。

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./extensions/benchmarks/models/xiaomi/config.json \
  --model-python /envs/model/bin/python --sim-python /envs/robocasa/bin/python \
  --model-devices 0,1,2,3,4,5,6,7 --workers-per-gpu 16 \
  --batch-size 8 --wait-ms 50 --output runs/robocasa365-xiaomi
```

正式 profile 使用 target50 × 50、pretrain、基础种子 7、步长 50、
256 像素相机和原生任务 horizon。Xiaomi 也可以运行 `--profile smoke`。
其他兼容模型同样可以选用 `xiaomi_official`。模型和评测协议独立选择。
单样本推理使用 `--batch-size 1 --workers-per-gpu 1 --wait-ms 0`。

模型保留已验证的 `.cuda().to(torch.bfloat16)` 转换，包括 RoPE
缓冲区。仿真器保留 EGL 析构上下文保护。旧渲染器在清理资源前绑定自己的
上下文，之后恢复活动上下文，避免 RGB 图像损坏。
动态合批可能改变浮点结果及全局 RNG 分配。
比较结果时应核对任务成功率。仅评测吞吐的工具位于
`tools/`，其输出不用于报告正式准确率。


## 结果与验证范围

运行中读取 `runs/evaluation/status.json` 的 `summary`；结束后读取 `summary.json`。统计包括逐 episode、逐任务及 benchmark 声明的维度。配置与调度参数保存在 `protocol.json`，实际清单在 `episode-plan.json`。详细解释见 [结果说明](../EVALUATION.zh-CN.md#10-输出与进度查询)。

已有真实仿真验证范围、硬件限制见 [SETUP](SETUP.zh-CN.md#验证范围)。源版本和许可见 [benchmark-source.json](benchmark-source.json)、[requirements.txt](requirements.txt)、`LICENSE.upstream` 和 `NOTICE.upstream`。

## 评测规划 + VLA + 验证系统

需要复用旧 XR365 分层 Graph 时，使用独立的 [RoboCasa 系统包](../systems/robocasa_hierarchical/README.zh-CN.md)。系统复用本 extension 当前的 Environment、Body、TaskAdapter 和协议，模型与决策逻辑分别存放在 models/ 和 systems/。纯 VLA 评测入口不变。
