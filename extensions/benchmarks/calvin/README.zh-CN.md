# CALVIN

[English](README.md) · [**简体中文**](README.zh-CN.md)

[全部 benchmark](../README.zh-CN.md) · [安装与资产](SETUP.zh-CN.md) · [快速开始](../QUICKSTART.zh-CN.md) · [并行与完整参数](../EVALUATION.zh-CN.md)

统一入口的 benchmark ID 为 **`calvin`**。该 extension 负责环境、任务、seed、horizon、观测/action 规格及成功判定；模型放在独立目录，通过 `--backend` 指定。

## 使用顺序

1. 按 [SETUP](SETUP.zh-CN.md) 安装固定版本的仿真环境，下载资产并设置路径。
2. 使用 SETUP 中的 fake-action 命令，确认环境可以推进、图像正常且无运行错误。
3. 按 [模型接口](../models/README.zh-CN.md) 接入模型，先做小任务子集验证。
4. 选择下面的 profile，按 [完整手册](../EVALUATION.zh-CN.md) 配置单卡或多卡评测。

## 选择评测协议

默认起点是 `eval`。每个 JSON 中的 `params` 为环境设置，`episodes_per_task` 为每任务运行次数；表中“未显式设置”表示需要查看 benchmark 实现的默认行为。

| Profile | 任务集 / 模式 | 每任务 episodes |
| --- | --- | ---: |
| [`eval`](configs/eval.json) | 见配置 | 1 |

`--profile` 支持上述名称或外部 JSON。需要改资产路径时复制配置并使用绝对路径；需要做小规模验证时使用 `--max-tasks` 和 `--episodes-per-task`。正式报告必须记录覆盖项，不能把子集标记成完整协议。

## 最小命令

完成安装后，在 RoboTenon 仓库根目录运行。`MODEL_PY`、`SIM_PY` 的设置见 [快速开始](../QUICKSTART.zh-CN.md)。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark calvin --profile eval \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/calvin-model-smoke
```

## 结果与验证范围

运行中读取 `runs/evaluation/status.json` 的 `summary`；结束后读取 `summary.json`。统计包括逐 episode、逐任务及 benchmark 声明的维度。配置与调度参数保存在 `protocol.json`，实际清单在 `episode-plan.json`。详细解释见 [结果说明](../EVALUATION.zh-CN.md#10-输出与进度查询)。

已有真实仿真验证范围、硬件限制见 [SETUP](SETUP.zh-CN.md#验证范围)。源版本和许可见 [benchmark-source.json](benchmark-source.json)、[requirements.txt](requirements.txt)、`LICENSE.upstream` 和 `NOTICE.upstream`。
