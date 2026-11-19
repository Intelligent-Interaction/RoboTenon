# robotwin 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Linux、Python 3.10、CUDA 工具链、SAPIEN 3.0.0b1。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

当前适配器固定从 **`/app/RoboTwin`** 访问源码，路径无法通过环境变量覆盖。请在仿真环境创建目录或将源码挂载到该路径。
```bash
git clone https://github.com/RoboTwin-Platform/RoboTwin.git /app/RoboTwin
git -C /app/RoboTwin checkout c3ddfa8b97d5519efa828b075999bd0006778e5e
cd /app/RoboTwin/assets
"$SIM_PY" _download.py
unzip objects.zip
unzip embodiments.zip
unzip background_texture.zip
cd /app/RoboTwin
"$SIM_PY" script/update_embodiment_config_path.py
cd "$ROBOTENON"
```
使用固定依赖，包括 CuRobo v0.7.8。先装 PyTorch，再使用 `--no-build-isolation` 构建 CuRobo。CUDA 扩展需 nvcc、Ninja 和匹配的 CUDA 工具包。资产包括机器人本体、物体和背景。移动源码后重新生成机器人本体资源路径。请核对所用上游版本的 SAPIEN/MPLib 兼容性补丁。

随附 profile 为仅评测的 qpos 流程关闭了专家可解性检查。启用 oracle 检查需要完整规划器。已有真实探针覆盖 `grab_roller/demo_clean`，无需训练轨迹，其他任务与配置仍需验证。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robotwin/configs/eval.json" "$ROBOTENON/local-profiles/robotwin.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robotwin --profile "$ROBOTENON/local-profiles/robotwin.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robotwin.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robotwin-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
