# molmospaces 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

固定资源栈建议使用 Python 3.12，仿真器为 MuJoCo。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

仿真器与资源包独立发布。请在包元数据允许的 Python 版本中安装固定的仿真依赖。已有服务器验证使用 Python 3.12。旧版 Python 3.11 安装方法可能不适用于所有资源包版本。
```bash
export MLSPACES_ASSETS_DIR="$ASSETS/molmospaces"
export MLSPACES_CACHE_DIR="$ASSETS/molmospaces-cache"
export MUJOCO_GL=egl
export JAX_PLATFORMS=cpu
mkdir -p "$MLSPACES_ASSETS_DIR" "$MLSPACES_CACHE_DIR"
"$SIM_PY" -c 'import molmo_spaces; from molmo_spaces.evaluation.benchmark_schema import load_all_episodes; print("asset initialization complete")'
find "$MLSPACES_ASSETS_DIR/benchmarks" -type d -name '*json_benchmark'
```
首次导入会触发上游资源下载和解压，从 [allenai/molmospaces](https://huggingface.co/datasets/allenai/molmospaces) 获取场景、物体、机器人和 benchmark 描述。保留下载缓存和解压后的资产，无需训练演示数据。

将 `params.benchmark_dir` 指向解压后的精确目录。默认 profile 使用 20260212 版 Franka PickAndPlace，共 200 个 episode。若资源清单中无该任务集，请安装匹配版本，或通过 `params.resource_manifest` 指定资源类型、来源及发布版本。不要私自替换为其他日期的 benchmark。目录缺失应按安装错误处理，并随结果记录实际资源版本。固定仿真器版本不足以固定资产。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/molmospaces/configs/pick_and_place.json" "$ROBOTENON/local-profiles/molmospaces.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark molmospaces --profile "$ROBOTENON/local-profiles/molmospaces.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/molmospaces.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/molmospaces-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
