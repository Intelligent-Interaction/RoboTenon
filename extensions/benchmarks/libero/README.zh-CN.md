# LIBERO

[English](README.md) · [**简体中文**](README.zh-CN.md)

[全部 benchmark](../README.zh-CN.md) · [安装与资产](SETUP.zh-CN.md) · [快速开始](../QUICKSTART.zh-CN.md) · [并行与完整参数](../EVALUATION.zh-CN.md)

统一入口的 benchmark ID 为 **`libero`**。该 extension 负责环境、任务、seed、horizon、观测/action 规格及成功判定；模型放在独立目录，通过 `--backend` 指定。

## 使用顺序

1. 按 [SETUP](SETUP.zh-CN.md) 安装固定版本的仿真环境，下载资产并设置路径。
2. 使用 SETUP 中的 fake-action 命令，确认环境可以推进、图像正常且无运行错误。
3. 按 [模型接口](../models/README.zh-CN.md) 接入模型，先做小任务子集验证。
4. 选择下面的 profile，按 [完整手册](../EVALUATION.zh-CN.md) 配置单卡或多卡评测。

## 选择评测协议

默认起点是 `smoke_test`。每个 JSON 中的 `params` 为环境设置，`episodes_per_task` 为每任务运行次数；表中“未显式设置”表示需要查看 benchmark 实现的默认行为。

| Profile | 任务集 / 模式 | 每任务 episodes |
| --- | --- | ---: |
| [`10`](configs/10.json) | libero_10 | 50 |
| [`all-0`](configs/all-0.json) | libero_spatial | 50 |
| [`all-1`](configs/all-1.json) | libero_object | 50 |
| [`all-2`](configs/all-2.json) | libero_goal | 50 |
| [`all-3`](configs/all-3.json) | libero_10 | 50 |
| [`goal`](configs/goal.json) | libero_goal | 50 |
| [`long`](configs/long.json) | libero_90 | 50 |
| [`object`](configs/object.json) | libero_object | 50 |
| [`smoke_test`](configs/smoke_test.json) | libero_spatial | 1 |
| [`spatial`](configs/spatial.json) | libero_spatial | 50 |

`--profile` 支持上述名称或外部 JSON。需要改资产路径时复制配置并使用绝对路径；需要做小规模验证时使用 `--max-tasks` 和 `--episodes-per-task`。正式报告必须记录覆盖项，不能把子集标记成完整协议。

OpenVLA-OFT 的 Spatial 专用接入使用 `--profile openvla_oft_spatial`，明确设置两路相机、状态、env_seed=0、原始四元数转换和夹爪直接执行；见 [adapter 说明](../models/openvla-oft/README.zh-CN.md)。该 profile 是50次/任务的完整配置，首次运行必须显式限制任务和 episode。

评测四个常用 suite 时，可用 `--profiles spatial object goal 10` 生成一份合并计划并统一调度，
不需要依次重启四次模型服务。`all-0` 至 `all-3` 是对应 suite 的别名，不要和上述 profile
重复加入同一份计划。

## 最小命令

完成安装后，在 RoboTenon 仓库根目录运行。`MODEL_PY`、`SIM_PY` 的设置见 [快速开始](../QUICKSTART.zh-CN.md)。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero --profile smoke_test \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/libero-model-smoke
```

## 结果与验证范围

运行中读取 `runs/evaluation/status.json` 的 `summary`；结束后读取 `summary.json`。统计包括逐 episode、逐任务及 benchmark 声明的维度。配置与调度参数保存在 `protocol.json`，实际清单在 `episode-plan.json`。详细解释见 [结果说明](../EVALUATION.zh-CN.md#10-输出与进度查询)。

Xiaomi Robotics 0 的四 suite 实测完成 2,000 / 2,000 个 episode，成功率为 **97.15%**，
8 张 H100 端到端耗时约 **20 分 35 秒**。不要只复制并发参数；完整配置、失败任务、实际
batch 分布和排错说明见 [Xiaomi Robotics 0 + LIBERO 复现指南](../../../docs/tutorials/benchmark-h100-xiaomi-libero.zh-CN.md)。

已有真实仿真验证范围、硬件限制见 [SETUP](SETUP.zh-CN.md#验证范围)。源版本和许可见 [benchmark-source.json](benchmark-source.json)、[requirements.txt](requirements.txt)、`LICENSE.upstream` 和 `NOTICE.upstream`。
