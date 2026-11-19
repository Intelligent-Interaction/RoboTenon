# calvin 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Python 3.10、PyBullet/EGL、Linux。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

递归克隆仿真器及其子模块。请保留 Git 元数据，CALVIN 会在运行时查询它。
```bash
git clone --recursive https://github.com/mees/calvin.git "$ASSETS/calvin"
git -C "$ASSETS/calvin" checkout fa03f01f19c65920e18cf37398a9ce859274af76
git -C "$ASSETS/calvin" submodule update --init --recursive
"$SIM_PY" -m pip install wheel 'setuptools==57.5.0'
"$SIM_PY" -m pip install --no-build-isolation -e "$ASSETS/calvin/calvin_env" -e "$ASSETS/calvin/calvin_models"
```
系统构建依赖包括 CMake、C/C++ 工具链、OpenBLAS 和 FFTW。安装扩展不会安装这些系统库。启动 Worker 之前应解决旧版 PyHash 或其他构建错误。

从固定版本的评测框架下载验证配置。该适配器无需训练轨迹。
```bash
git clone https://github.com/allenai/vla-evaluation-harness.git "$ASSETS/harness-reference"
git -C "$ASSETS/harness-reference" checkout 6cc3e1bd9fc4bed26fb0a3aabd342c5fed11ca09
```
将 `params.dataset_path` 设为 `$ASSETS/harness-reference/docker/calvin_validation_data`，确认其中保存有完整的 `.hydra` 配置和隐藏文件。若使用完整数据集，按 [CALVIN 数据集指南](https://github.com/mees/calvin/blob/fa03f01f19c65920e18cf37398a9ce859274af76/dataset/README.md)准备，并指向解压后的 `validation` 目录。CALVIN 评估连续子任务，应同时报告序列完成情况与成功率。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/calvin/configs/eval.json" "$ROBOTENON/local-profiles/calvin.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark calvin --profile "$ROBOTENON/local-profiles/calvin.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/calvin.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/calvin-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
