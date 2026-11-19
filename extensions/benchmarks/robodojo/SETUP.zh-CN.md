# robodojo 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Linux、RTX GPU、Isaac Sim 5.1，使用官方 Python 3.11 环境。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

使用固定版本的上游 Docker 或构建环境安装 Isaac 及二进制依赖，仅安装本扩展的 requirements 不足以运行仿真。
```bash
git clone --recursive https://github.com/RoboDojo-Benchmark/RoboDojo.git "$ASSETS/RoboDojo"
git -C "$ASSETS/RoboDojo" checkout ee67a1468510da7624a089164402359f2afc72c8
git -C "$ASSETS/RoboDojo" submodule update --init --recursive
cd "$ASSETS/RoboDojo"
bash scripts/init_assets.sh
cd "$ROBOTENON"
export ROBODOJO_ASSETS="$ASSETS/RoboDojo/Assets"
```
脚本通过 Git LFS 下载 [RoboDojo 资产](https://huggingface.co/datasets/RoboDojo-Benchmark/RoboDojo)，应先安装 `git-lfs`。场景、机器人与物体等资产约需 90 GiB 磁盘，具体取决于上游配置。下载或使用前请阅读仓库、数据集和 NVIDIA 条款。设置 profile 的 `params.root` 为仿真环境或容器中可见的源码目录，并将资产挂载到匹配位置。

按[固定版本的 Dockerfile](https://github.com/RoboDojo-Benchmark/RoboDojo/blob/ee67a1468510da7624a089164402359f2afc72c8/Dockerfile)安装 Isaac。A100 无法验证所需 RTX 渲染。Isaac 应用的生命周期为进程级，先用单任务、单 Worker 启动。当前环境中尚**未完成**真实 Isaac 仿真验证，接口测试不代表渲染已验证。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/robodojo/configs/smoke_test.json" "$ROBOTENON/local-profiles/robodojo.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robodojo --profile "$ROBOTENON/local-profiles/robodojo.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/robodojo.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/robodojo-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

目前仅完成接口与配置测试。真实仿真、图像和成功信号仍需在 RTX 硬件上验证。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
