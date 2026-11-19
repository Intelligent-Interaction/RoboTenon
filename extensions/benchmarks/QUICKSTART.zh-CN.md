# 快速开始：完成第一次评测

[English](QUICKSTART.md) · [**简体中文**](QUICKSTART.zh-CN.md)

[返回首页](README.zh-CN.md) · [安装说明](SETUP.zh-CN.md) · [完整参数与并行](EVALUATION.zh-CN.md)

这份指南只完成一件事：用统一入口跑通一个小规模评测。以 LIBERO-Plus 为示例，其他 benchmark 使用相同流程，更换对应 ID、profile、仿真环境和 fake 配置即可。

## 1. 选择环境并完成安装

从 [benchmark 索引](README.zh-CN.md#benchmark-索引) 选择环境，先阅读其 SETUP 并准备资产。LIBERO-Plus 对应 [安装指南](libero_plus/SETUP.zh-CN.md)，需要独立的 Python 3.10 仿真环境、固定源码和 Plus 资产。

在运行服务器上设置真实的绝对路径：

```bash
export ROBOTENON=/opt/RoboTenon
export MODEL_PY=/opt/envs/model/bin/python
export SIM_PY=/opt/envs/libero-plus/bin/python
export ASSETS=/data/benchmark-assets
cd "$ROBOTENON"
```

这些是示例路径，不会自动创建 Python 环境。按照 [共享安装步骤](SETUP.zh-CN.md) 在两个环境中安装框架和共享 runtime，在 SIM_PY 安装选定 benchmark，在 MODEL_PY 安装模型依赖。服务器需要的资产直接在服务器下载。

## 2. 用假动作验证安装

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_plus --profile spatial \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --backend-config ./extensions/benchmarks/models/fake/configs/libero_plus.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-gpu 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/libero-plus-fake-smoke
```

若配置包含默认容器路径，应复制 JSON、改成实际绝对路径，再将 `--profile` 换成这个文件。每次用新的 output 目录。

检查：进程正常退出、无 episode 错误、环境步数增加、观测图像正确。fake action 常常不能完成任务，成功率为0不代表安装失败。图像不变化也可能是零动作的预期结果，图像推进验证需使用会产生运动的动作。

## 3. 换成自己的模型

按照 [模型接口](models/README.zh-CN.md) 实现独立目录中的 `vla.py`，并明确声明支持 `libero_plus`。仅替换命令中的：

```text
--backend /data/models/my-model/vla.py
--backend-config /data/models/my-model/config.json
```

模型需处理图像、状态、动作反归一化和机器人 action 转换。现有 Xiaomi 后端仅声明支持 RoboCasa365，不能仅更换 benchmark 名就用于 LIBERO-Plus。

先保留任务子集和单 Worker，确认模型输入输出正确。

## 4. 再增加并发

后端确实支持 batch 后，把执行参数改为：

```text
--model-devices 0,1
--workers-per-gpu 4
--batch-size 4
--wait-ms 50
```

这表示2个模型副本、8个仿真 Worker，每个模型服务独立组 batch。全局队列分配 episode，模型请求仍发给 Worker 固定绑定的 GPU。更多配置见 [并行能力](EVALUATION.zh-CN.md#4-并行能力总览)。

## 5. 运行正式协议并查看结果

选择 benchmark README 中的正式 profile，移除用于安装验证的 `--max-tasks`、`--episodes-per-task` 覆盖。LIBERO-Plus 的四个 suite 要分别选择 `all-0` 至 `all-3`；单个 `all-0` 不是完整四套评测。

| 文件 | 用途 |
| --- | --- |
| `protocol.json` | 实际协议、模型配置、RNG、执行参数 |
| `episode-plan.json` | 实际 episode 清单 |
| `runs/evaluation/status.json` | 当前进度；运行中读取其中的 `summary` |
| `runs/evaluation/summary.json` | 最终结果；运行中可能尚未更新 |

成功率、错误、重试和跨任务汇总的解释见 [结果章节](EVALUATION.zh-CN.md#10-输出与进度查询)。完整路径与命令可从生成配置复核，不要把 smoke 成绩当成正式准确率。
