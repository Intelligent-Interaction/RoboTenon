# libero_pro 安装、资产与首次评测

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

## 运行环境

Python 3.10、MuJoCo 3.2.3，使用独立的 LIBERO 分支。

先阅读[通用安装步骤](../SETUP.zh-CN.md)，设置 `ROBOTENON`、`ASSETS`、`SIM_PY` 和 `MODEL_PY`。使用独立的仿真环境，安装该扩展的依赖，再完成下方的专用步骤。在仿真服务器直接运行命令和下载资产。**安装依赖前**请先阅读下方关于构建顺序的例外说明。

## 资产与仿真器安装

每个 LIBERO 分支应使用独立环境，**并配置独立的 `HOME` 和 LIBERO 配置文件**。各分支导入的包都叫 `libero`。
```bash
git clone https://github.com/Zxy-MLlab/LIBERO-PRO.git "$ASSETS/libero_pro"
git -C "$ASSETS/libero_pro" checkout eafdb809426b13153aa1e4c42d6601844217dfec
export LIBERO_SOURCE="$ASSETS/libero_pro"
"$SIM_PY" -m pip install -e "$LIBERO_SOURCE"
mkdir -p "$HOME/.libero"
cat > "$HOME/.libero/config.yaml" <<EOF
benchmark_root: $LIBERO_SOURCE/libero/libero
bddl_files: $LIBERO_SOURCE/libero/libero/bddl_files
init_states: $LIBERO_SOURCE/libero/libero/init_files
datasets: $LIBERO_SOURCE/libero/datasets
assets: $LIBERO_SOURCE/libero/libero/assets
EOF
export MUJOCO_GL=egl
export NUMBA_DISABLE_JIT=1
```
仿真进程会读取该配置。请保留源码 checkout，可编辑安装和 XML 资产会引用它。评测需要 BDDL、网格/纹理和初始状态。在线评测器无需训练演示 HDF5 文件。

## 配置本地路径

修改资产路径前先复制 profile。JSON 不会展开 `$ASSETS` 或 `~`。

```bash
mkdir -p "$ROBOTENON/local-profiles"
cp "$ROBOTENON/extensions/benchmarks/libero_pro/configs/eval.json" "$ROBOTENON/local-profiles/libero_pro.json"
```

在副本的 `params` 中使用上述绝对路径。做协议对比时保留原任务 ID、种子、步数上限和观测设置。下方的快速安装检查限制任务和 episode 数，但单个任务仍可能运行到原定的完整步数上限。

## 使用假动作进行冒烟测试

安装完成后，在 RoboTenon 仓库根目录运行下列命令。

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_pro --profile "$ROBOTENON/local-profiles/libero_pro.json" \
  --backend "$ROBOTENON/extensions/benchmarks/models/fake/vla.py" \
  --backend-config "$ROBOTENON/extensions/benchmarks/models/fake/configs/libero_pro.json" \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --max-tasks 1 --episodes-per-task 1 \
  --workers-per-gpu 1 --batch-size 1 --wait-ms 50 \
  --output "$ROBOTENON/runs/libero_pro-install-check"
```

需要 GPU 的环境应增加 `--model-devices 0`，并确认仿真进程可以访问驱动和渲染设备。假动作验证接口，策略成功率为零通常符合预期。按[结果核验步骤](../SETUP.zh-CN.md#核验运行结果)检查 episode 错误、步数、观测和图像内容。

## 后续评测

在 [README](README.zh-CN.md) 中选择目标协议，将假动作替换为实际的[模型后端](../models/README.zh-CN.md)。并行执行、监控和结果汇总见[评测手册](../EVALUATION.zh-CN.md)。上述冒烟测试子集仅用于检查安装。

## 验证范围

已有真实仿真探针对选定任务的动作、图像和信号进行了检查。其范围不包括全新机器安装和全套成功率验证。任务和种子范围见[原生一致性报告](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)。

固定的源版本与许可见[依赖](requirements.txt)、[来源记录](benchmark-source.json)和[原生协议](NATIVE_EVALUATION.zh-CN.md)。比较结果时，请使用记录的源码版本，避免直接替换为当前上游 main 分支。
