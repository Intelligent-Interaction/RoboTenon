# rlbench 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Python 3.10、CoppeliaSim Edu 4.1.0、Linux X11/GLX。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

使用指定的兼容仿真器版本。采用 Edu 学术环境前，请阅读 [CoppeliaSim 许可条款](https://www.coppeliarobotics.com/)及 [RLBench 许可证](https://github.com/stepjam/RLBench)。
```bash
curl -fL --retry 5 https://downloads.coppeliarobotics.com/V4_1_0/CoppeliaSim_Edu_V4_1_0_Ubuntu20_04.tar.xz -o "$ASSETS/coppeliasim.tar.xz"
mkdir -p "$ASSETS/coppeliasim"
tar -xJf "$ASSETS/coppeliasim.tar.xz" -C "$ASSETS/coppeliasim" --strip-components=1
export COPPELIASIM_ROOT="$ASSETS/coppeliasim"
export LD_LIBRARY_PATH="$COPPELIASIM_ROOT:${LD_LIBRARY_PATH:-}"
export QT_QPA_PLATFORM_PLUGIN_PATH="$COPPELIASIM_ROOT/platforms"
export QT_PLUGIN_PATH="$COPPELIASIM_ROOT"
export QT_QPA_PLATFORM=xcb
"$SIM_PY" -m pip install cffi
"$SIM_PY" -m pip install --no-build-isolation 'git+https://github.com/stepjam/PyRep.git@8f420be8064b1970aae18a9cfbc978dfb15747ef'
"$SIM_PY" -m pip install --no-deps 'git+https://github.com/stepjam/RLBench.git@02720bba4c73fe02eb75df946b8791b806028a9d'
```
任务场景和模型资产随 RLBench、PyRep 及仿真器发布，在线运行无需离线演示数据集。安装 Xvfb 和 X11/GLX 系统库。在无显示器主机上，为评测命令加上 `xvfb-run -a -s '-screen 0 1280x1024x24'`。避免 Qt `offscreen`，该模式可能初始化成功却输出黑帧。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/rlbench/configs/eval.json" "$ROBOTENON/local-profiles/rlbench.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark rlbench --profile "$ROBOTENON/local-profiles/rlbench.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/rlbench.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/rlbench-install-check"
```

使用上述 Xvfb 前缀。需要 GPU 时增加 `--model-devices 0`，并检查仿真进程可访问驱动和渲染设备。假动作只验证接口，任务成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测与图像。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
