# simpler 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Linux、Python 3.10、SAPIEN/Vulkan、NumPy 1.24.4。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

安装 requirements.txt 固定的 ManiSkill2_real2sim 和 SimplerEnv，其中包含机器人、任务定义和随附资产。确保 Worker 可访问缓存和源码数据。在离线评测前分别初始化所选任务类型。视觉变体的额外场景与覆盖图像见[固定版本的 SimplerEnv 指南](https://github.com/simpler-env/SimplerEnv/tree/4ab7178e83e84ee06894034ec6dbf9e7aad1e882)。在线仿真无需下载 Open X-Embodiment 等训练数据。

保留包含非包资源 `data/` 的源码目录，单独安装 wheel 可能缺少它。
```bash
git clone https://github.com/simpler-env/ManiSkill2_real2sim.git "$ASSETS/ManiSkill2_real2sim"
git -C "$ASSETS/ManiSkill2_real2sim" checkout ef7a4d4fdf4b69f2c2154db5b15b9ac8dfe10682
"$SIM_PY" -m pip install -e "$ASSETS/ManiSkill2_real2sim"
export MS2_REAL2SIM_ASSET_DIR="$ASSETS/ManiSkill2_real2sim/data"
```
确认存在 `data/custom`、`data/hab2_bench_assets` 和 `data/real_inpainting`。固定版本的[资产解析器](https://github.com/simpler-env/ManiSkill2_real2sim/blob/ef7a4d4fdf4b69f2c2154db5b15b9ac8dfe10682/mani_skill2_real2sim/__init__.py)优先读取 `MS2_REAL2SIM_ASSET_DIR`，然后才使用备用路径。

依赖固定为 OpenCV 4.11.0.86。OpenCV 4.12 在该 Python 环境中要求 NumPy 2，与指定的 NumPy 1.24.4 冲突，请避免单独升级。

Visual Matching（`*_vm-*`）和 Variant Aggregation（`*_va-*`）是不同的协议。完整对比需选取全部目标分片。`google_robot_drawer_va-0` 的 smoke 只覆盖一个子集。无显示器的推理机器仍需支持 Vulkan。CPU Lavapipe 环境依赖兼容的软件 Vulkan 实现，不能据此推断 NVIDIA 渲染性能。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/simpler/configs/google_robot_drawer_va-0.json" "$ROBOTENON/local-profiles/simpler.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark simpler --profile "$ROBOTENON/local-profiles/simpler.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/simpler.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/simpler-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
