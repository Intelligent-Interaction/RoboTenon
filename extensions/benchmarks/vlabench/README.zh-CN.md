# VLABench

[English](README.md) · [**简体中文**](README.zh-CN.md)

[全部 benchmark](../README.zh-CN.md) · [安装与资产](SETUP.zh-CN.md) · [快速开始](../QUICKSTART.zh-CN.md) · [并行与完整参数](../EVALUATION.zh-CN.md)

统一入口的 benchmark ID 为 **`vlabench`**。该 extension 负责环境、任务、seed、horizon、观测/action 规格及成功判定；模型放在独立目录，通过 `--backend` 指定。

## 使用顺序

1. 按 [SETUP](SETUP.zh-CN.md) 安装固定版本的仿真环境，下载资产并设置路径。
2. 使用 SETUP 中的 fake-action 命令，确认环境可以推进、图像正常且无运行错误。
3. 按 [模型接口](../models/README.zh-CN.md) 接入模型，先做小任务子集验证。
4. 选择下面的 profile，按 [完整手册](../EVALUATION.zh-CN.md) 配置单卡或多卡评测。

## 选择评测协议

Xiaomi-Robotics-1 使用 8 张 H100 完成全量评测，最终任务宏平均成功率为 **60.84%**，总耗时约 **1 小时 54 分钟**。完整配置、50 项任务结果及吞吐见 [Xiaomi + VLABench 测试报告](../../../docs/tutorials/benchmark-h100-xiaomi-vlabench.zh-CN.md)。

默认起点是 `eval`。每个 JSON 中的 `params` 为环境设置，`episodes_per_task` 为每任务运行次数；表中“未显式设置”表示需要查看 benchmark 实现的默认行为。

| Profile | 任务集 / 模式 | 每任务 episodes |
| --- | --- | ---: |
| [`eval`](configs/eval.json) | ['select_fruit', 'select_toy', 'select_drink'] | 3 |
| [`full`](configs/full.json) | Track 1、2、3、4、6，共 50 项 | 最多 50；实际总计 2460 |

`full` profile 从 `$VLABENCH_ROOT/configs/evaluation/tracks` 读取 Track 1、2、3、4、6，共 50 个 task-track 条目，每项最多使用 50 个配置。所用资产版本的 Track 2 `insert_flower` 仅有 10 个配置，因此完整运行共 2460 项。

`--profile` 支持上述名称或外部 JSON。需要改资产路径时复制配置并使用绝对路径；需要做小规模验证时使用 `--max-tasks` 和 `--episodes-per-task`。正式报告必须记录覆盖项，不能把子集标记成完整协议。

## 资产与模型下载途径

以下资产地址来自固定版本的 [官方下载脚本](https://github.com/OpenMOSS/VLABench/blob/cf588fe60c0c7282174fe979f5913170cfe69017/scripts/download_assets.py)。下载应在仿真服务器执行；完整安装步骤见 [SETUP](SETUP.zh-CN.md)。

| 内容 | 来源 | 必需性 / 放置位置 |
| --- | --- | --- |
| VLABench 源码、机器人和基础资产、Track 配置 | [OpenMOSS/VLABench](https://github.com/OpenMOSS/VLABench)，固定到 `cf588fe60c0c7282174fe979f5913170cfe69017` | 必需；Track 文件在包内 `configs/evaluation/tracks/` |
| 物体模型与纹理压缩包 | [Google Drive：obj.zip](https://drive.google.com/file/d/1ldEMZua2OzXHJTYTCP0IGVU1aFYBCMu-/view) | 必需；下载后解压至 `$VLABENCH_ROOT/assets`，产生/补齐 `obj/` |
| 场景资产压缩包 | [Google Drive：scene.zip](https://drive.google.com/file/d/1KdReRkibJClBHHD32jz_wTkaBzhEJ9Kw/view) | 必需；下载后解压至 `$VLABENCH_ROOT/assets`，产生/补齐 `scenes/` |
| Xiaomi VLABench 权重和 processor | [Hugging Face：Xiaomi-Robotics-1-VLABench](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-VLABench) | 测试 Xiaomi 时必需；使用该专用 checkpoint，不用 RoboCasa365 权重替代 |

`VLABENCH_ROOT` 指向包含 `assets` 和 `configs` 的 **Python 包目录**：如果 checkout 在 `/data/assets/VLABench`，应设为 `/data/assets/VLABench/VLABench`。

```bash
export ASSETS=/data/assets
git clone https://github.com/OpenMOSS/VLABench.git "$ASSETS/VLABench"
git -C "$ASSETS/VLABench" checkout cf588fe60c0c7282174fe979f5913170cfe69017
export VLABENCH_ROOT="$ASSETS/VLABench/VLABench"
mkdir -p "$VLABENCH_ROOT/assets"
# 先按 SETUP 安装 VLABench 及 gdown 等依赖。
"$SIM_PY" "$ASSETS/VLABench/scripts/download_assets.py" --choice all

hf download XiaomiRobotics/Xiaomi-Robotics-1-VLABench \
  --local-dir /data/models/Xiaomi-Robotics-1-VLABench
```

官方脚本也支持 `--choice asset` 和 `--choice scene`，可分别重试失败的包。需要并行下载时，将上述 `--choice all` 替换为：

```bash
"$SIM_PY" "$ASSETS/VLABench/scripts/download_assets.py" --choice asset &
asset_pid=$!
"$SIM_PY" "$ASSETS/VLABench/scripts/download_assets.py" --choice scene &
scene_pid=$!
wait "$asset_pid"; asset_status=$?
wait "$scene_pid"; scene_status=$?
test "$asset_status" -eq 0 && test "$scene_status" -eq 0
```

Google Drive 配额或网络失败时，可用表中链接手工下载两个 ZIP，并检查压缩包完整性后解压到相同目录。不要把下载到的 HTML 错误页当作 ZIP，也不要额外嵌套一层 `assets`。最终包目录下应有 `assets/base`、`assets/robots`、`assets/obj`、`assets/scenes`；同时检查 Track 6 所引用纹理等文件是否齐全。目录存在不代表资产完整，需完成 [fake-action 安装检查](SETUP.zh-CN.md#使用假动作进行冒烟测试)并验证目标 Track 的 reset。纯推理评测不需要下载训练 demonstration 数据集。

## 最小命令

完成安装后，在 RoboTenon 仓库根目录运行。`MODEL_PY`、`SIM_PY` 的设置见 [快速开始](../QUICKSTART.zh-CN.md)。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark vlabench --profile eval \
  --backend /data/models/my-model/vla.py \
  --backend-config /data/models/my-model/config.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/vlabench-model-smoke
```

## 结果与验证范围

运行中读取 `runs/evaluation/status.json` 的 `summary`；结束后读取 `summary.json`。统计包括逐 episode、逐任务及 benchmark 声明的维度。配置与调度参数保存在 `protocol.json`，实际清单在 `episode-plan.json`。详细解释见 [结果说明](../EVALUATION.zh-CN.md#10-输出与进度查询)。

已有真实仿真验证范围、硬件限制见 [SETUP](SETUP.zh-CN.md#验证范围)。源版本和许可见 [benchmark-source.json](benchmark-source.json)、[requirements.txt](requirements.txt)、`LICENSE.upstream` 和 `NOTICE.upstream`。
