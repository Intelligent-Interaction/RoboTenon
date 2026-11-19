# kinetix 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Python 3.11、JAX，支持 CPU 或 CUDA。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

安装 Kinetix v3.0.0，并下载 RTC 世界 JSON 文件。这些是小型关卡源资产。
```bash
git clone https://github.com/Physical-Intelligence/real-time-chunking-kinetix.git "$ASSETS/rtc"
git -C "$ASSETS/rtc" checkout 9296f31d62d5bfeb5779dcb2f9bcf71ca37f448b
"$SIM_PY" -m pip install 'git+https://github.com/FLAIROx/Kinetix.git@v3.0.0'
```
在同步及实时 profile 中，将 `params.rtc_worlds_dir` 设为 `$ASSETS/rtc/worlds`。CPU 使用 `jax` 并设置 `render: "cpu"`。requirements 中的 `jax[cuda12]` 只适用于 Linux CUDA，在 macOS 或纯 CPU 环境应移除。CUDA 环境需检查驱动兼容性后安装对应 extra。

README 的实时章节说明 live Graph 时钟和陈旧动作。首次 JAX 编译可能使实际频率低于目标值，需检查 `rt_metrics` 以及 episode 结果。实时与同步使用不同执行协议，应分别报告。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/kinetix/configs/eval.json" "$ROBOTENON/local-profiles/kinetix.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark kinetix --profile "$ROBOTENON/local-profiles/kinetix.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/kinetix.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/kinetix-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。
[实时验证](../../../docs/changes/kinetix-live-unified-2026-09-29.zh-CN.md)还覆盖在 CPU 上运行 live Graph 的情况。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
