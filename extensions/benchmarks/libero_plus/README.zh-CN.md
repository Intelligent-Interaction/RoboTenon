# LIBERO-Plus

[English](README.md) · [**简体中文**](README.zh-CN.md)

[全部 benchmark](../README.zh-CN.md) · [安装与资产](SETUP.zh-CN.md) · [快速开始](../QUICKSTART.zh-CN.md) · [并行与完整参数](../EVALUATION.zh-CN.md)

统一入口的 benchmark ID 为 **`libero_plus`**。该 extension 负责环境、任务、seed、horizon、观测/action 规格及成功判定；模型放在独立目录，通过 `--backend` 指定。

## 使用顺序

1. 按 [SETUP](SETUP.zh-CN.md) 安装固定版本的仿真环境，下载资产并设置路径。
2. 使用 SETUP 中的 fake-action 命令，确认环境可以推进、图像正常且无运行错误。
3. 按 [模型接口](../models/README.zh-CN.md) 接入模型，先做小任务子集验证。
4. 选择下面的 profile，按 [完整手册](../EVALUATION.zh-CN.md) 配置单卡或多卡评测。

## 选择评测协议

默认起点是 `all-0`。每个 JSON 中的 `params` 为环境设置，`episodes_per_task` 为每任务运行次数；表中“未显式设置”表示需要查看 benchmark 实现的默认行为。

| Profile | 任务集 / 模式 | 每任务 episodes |
| --- | --- | ---: |
| [`all-0`](configs/all-0.json) | libero_spatial | 1 |
| [`all-1`](configs/all-1.json) | libero_object | 1 |
| [`all-2`](configs/all-2.json) | libero_goal | 1 |
| [`all-3`](configs/all-3.json) | libero_10 | 1 |
| [`spatial`](configs/spatial.json) | libero_spatial | 1 |

`all-0` 至 `all-3` 分别为 spatial、object、goal、libero_10；`spatial` 与 `all-0` 重复，完整评测不要重复计数。完整评测可用 `--profiles all-0 all-1 all-2 all-3` 生成一份合并计划，由全局队列统一 shuffle 和调度，最终按 episode 数加权汇总。也可以分别运行四份配置，但必须保留 suite 维度并按实际 episode 数汇总。可在外部 JSON 的 `params` 设置 `category` 和 `difficulty_level` 做扰动维度子集。

`--profile` 支持上述名称或外部 JSON。需要改资产路径时复制配置并使用绝对路径；需要做小规模验证时使用 `--max-tasks` 和 `--episodes-per-task`。正式报告必须记录覆盖项，不能把子集标记成完整协议。

## 最小命令

完成安装后，在 RoboTenon 仓库根目录运行。`MODEL_PY`、`SIM_PY` 的设置见 [快速开始](../QUICKSTART.zh-CN.md)。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_plus --profile all-0 \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/libero_plus-model-smoke
```

## 结果与验证范围

运行中读取 `runs/evaluation/status.json` 的 `summary`；结束后读取 `summary.json`。统计包括逐 episode、逐任务及 benchmark 声明的维度。配置与调度参数保存在 `protocol.json`，实际清单在 `episode-plan.json`。详细解释见 [结果说明](../EVALUATION.zh-CN.md#10-输出与进度查询)。

Xiaomi Robotics 0 在四个 suite、10,030 个案例上的完整实测成功率为 **74.77%**，8 张 H100
端到端耗时 **2 小时 7 分 57 秒**。完整配置、分 suite / 扰动类别 / 难度结果及 batch 分布见
[Xiaomi Robotics 0 + LIBERO-Plus 全量评测](../../../docs/tutorials/benchmark-h100-xiaomi-libero-plus.zh-CN.md)。

已有真实仿真验证范围、硬件限制见 [SETUP](SETUP.zh-CN.md#验证范围)。源版本和许可见 [benchmark-source.json](benchmark-source.json)、[requirements.txt](requirements.txt)、`LICENSE.upstream` 和 `NOTICE.upstream`。
