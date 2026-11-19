# behavior1k 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Linux、RTX GPU、Isaac Sim 4.5、OmniGibson 3.7.2、Python 3.10。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

按 [BEHAVIOR v3.7.2 安装指南](https://github.com/StanfordVL/BEHAVIOR-1K/tree/v3.7.2) 安装 Isaac 和 OmniGibson，然后在同一仿真环境安装扩展依赖。该 Isaac 栈的要求与 RoboDojo 不同。

必需资产包括 OmniGibson 机器人资产、BEHAVIOR 场景和物体数据，以及 **2025 挑战赛任务实例**。请阅读[数据集条款](https://behavior.stanford.edu/dataset)和 NVIDIA Isaac Sim EULA。首次初始化会请求接受条款，然后调用 `omnigibson.utils.asset_utils` 的 `download_omnigibson_robot_assets`、`download_behavior_1k_assets` 与 `download_2025_challenge_task_instances`。先用持久、可写的数据卷完成交互式初始化，再安排无人值守的 Worker。

下载位置为 `omnigibson.macros.gm.DATA_PATH`。检查其中是否同时含有 `2025-challenge-task-instances` 与机器人、物体和场景资产包，仅有标记目录无法证明下载完整。容器重启时应保留该目录。在线适配器无需训练演示数据。

接受适用的 NVIDIA 条款后，再在仿真环境设置所需 EULA 变量。无显示器运行时设为 `OMNIGIBSON_HEADLESS=1`。先用随附的单任务 profile 与一个 Worker 运行。目前的 A100 机器缺少所需的 RTX 渲染平台，因此**尚未验证真实 Isaac 渲染**。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/behavior1k/configs/eval.json" "$ROBOTENON/local-profiles/behavior1k.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark behavior1k --profile "$ROBOTENON/local-profiles/behavior1k.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/behavior1k.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/behavior1k-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

目前仅完成接口与配置测试。真实仿真、图像和成功信号仍需在 RTX 硬件上验证。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
