# robocasa 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Python 3.10、Linux MuJoCo/EGL。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

RoboCasa 0.2 与 RoboCasa365 1.0.1 使用不同的环境和资产。固定 RoboCasa 与 robosuite 两者的版本。
```bash
git clone https://github.com/robocasa/robocasa.git "$ASSETS/robocasa"
git -C "$ASSETS/robocasa" checkout v0.2
"$SIM_PY" -m pip install 'git+https://github.com/ARISE-Initiative/robosuite.git@v1.5.0'
"$SIM_PY" -m pip install -e "$ASSETS/robocasa"
cd "$ASSETS/robocasa"
"$SIM_PY" robocasa/scripts/setup_macros.py
"$SIM_PY" robocasa/scripts/download_kitchen_assets.py
cd "$ROBOTENON"
export MUJOCO_GL=egl
```
资产下载器按配置的仿真资产根目录安装厨房布局、纹理、物体和固定设施。按提示操作并确认下载全部完成。两个任务的 smoke 资产子集不足以运行全套任务。在线 rollout 无需演示数据集，模型权重放在模型环境中。

保留扩展提供的渲染器生命周期修复。多 GPU 运行时，统一运行器分别绑定仿真 EGL 与模型设备，避免用全局配置覆盖各 Worker。检查 reset 后和执行动作后的图像。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robocasa/configs/smoke.json" "$ROBOTENON/local-profiles/robocasa.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa --profile "$ROBOTENON/local-profiles/robocasa.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robocasa.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robocasa-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
