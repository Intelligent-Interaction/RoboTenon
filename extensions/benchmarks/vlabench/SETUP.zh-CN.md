# vlabench 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Python 3.10、MuJoCo 3.2.2、dm_control 1.0.22。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

使用固定源码版本提供的下载器获取任务网格、纹理和场景资产。
```bash
git clone https://github.com/OpenMOSS/VLABench.git "$ASSETS/VLABench"
git -C "$ASSETS/VLABench" checkout cf588fe60c0c7282174fe979f5913170cfe69017
"$SIM_PY" -m pip install -e "$ASSETS/VLABench"
export VLABENCH_ROOT="$ASSETS/VLABench/VLABench"
cd "$ASSETS/VLABench"
"$SIM_PY" scripts/download_assets.py
cd "$ROBOTENON"
export MUJOCO_GL=egl
```
上游下载器从 Google Drive 拉取物体和场景归档。请在存放仿真资产的服务器上运行，核对退出状态和解压后的文件。配额或网络错误后生成的空目录无法证明安装完成。仅推进环境时，演示数据集和模型权重均可省略。

上游默认设置不保证 reset 可复现。扩展提供显式种子适配，对比运行时请记录是否开启。设种探针的覆盖范围见原生一致性报告。

## 配置本地路径

### 复现 Xiaomi 全量评测的运行时优化

实测运行在上游八个 Python 文件中使用 Open3D 延迟导入。具体变更见 [patches/lazy-open3d.patch](patches/lazy-open3d.patch)。安装后，在固定的 VLABench 源码上应用一次。

```bash
git -C "$ASSETS/VLABench" apply --check "$ROBOTENON/extensions/benchmarks/vlabench/patches/lazy-open3d.patch"
git -C "$ASSETS/VLABench" apply "$ROBOTENON/extensions/benchmarks/vlabench/patches/lazy-open3d.patch"
```

已打补丁的目录无需重复应用。报告结果时记录该上游改动。模型侧要枚举配置，也需直接访问源码，但不应导入仿真器。

```bash
export PYTHONPATH="$ASSETS/VLABench${PYTHONPATH:+:$PYTHONPATH}"
export ROBOTENON_LOCAL_RUNTIME_ROOT=/tmp/robotenon-benchmark
```

模型服务指标保存在 `$ROBOTENON_LOCAL_RUNTIME_ROOT/<run-id>/batch-metrics`。使用唯一的 run ID，本地文件不会自动归档到共享存储。回收节点前保存最终指标。Episode 结果仍写入评测器输出目录。模型与仿真器使用独立解释器，完整执行参数见[全量评测报告](../../../docs/tutorials/benchmark-h100-xiaomi-vlabench.zh-CN.md)。

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/vlabench/configs/eval.json" "$ROBOTENON/local-profiles/vlabench.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark vlabench --profile "$ROBOTENON/local-profiles/vlabench.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/vlabench.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/vlabench-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
