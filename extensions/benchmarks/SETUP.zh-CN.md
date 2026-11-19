# 安装并验证评测环境

[English](SETUP.md) · [**简体中文**](SETUP.zh-CN.md)

完整评测与并行使用手册：[多 GPU、动态 batch、调度、实时模式、故障处理和结果统计](EVALUATION.zh-CN.md)。


## 选择独立的运行环境

除非所选 benchmark 明确支持 CPU 或 macOS，否则请在 Linux 上运行 GPU 仿真栈。MuJoCo、SAPIEN、Isaac、NumPy 和 torch 的版本要求可能冲突，因此各仿真器应使用独立的环境。每个 benchmark 的 SETUP.md 列出了依赖与资产，固定的源码版本见 requirements 和来源记录文件。

在运行仿真器的服务器上设置绝对路径。请按实际安装位置修改以下示例。

```bash
export ROBOTENON=/opt/RoboTenon
export ASSETS=/data/benchmark-assets
export SIM_PY=/opt/envs/selected-simulator/bin/python
export MODEL_PY=/opt/envs/selected-model/bin/python
mkdir -p "$ASSETS"
cd "$ROBOTENON"
```

按对应指南指定的 Python 版本创建环境。RoboTenon 需要 Python 3.10 或更高版本。执行命令前，`SIM_PY` 与 `MODEL_PY` 必须指向已有解释器。模型环境同时运行协调器。简单的假动作检查可共用环境，实际模型建议使用独立环境。

先查阅所选 benchmark 的构建顺序，尤其是 PyRep、CALVIN 和 CuRobo。将 `BENCHMARK` 设为目录中列出的 ID 后执行安装。

```bash
export BENCHMARK=libero
"$SIM_PY" -m pip install -r "extensions/benchmarks/$BENCHMARK/requirements.txt"
"$SIM_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support -e "extensions/benchmarks/$BENCHMARK"
"$MODEL_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support
"$SIM_PY" -m pip check
"$MODEL_PY" -m robotenon_benchmark_support.evaluate --help
```

模型的 torch 和 transformers 依赖应装在模型环境中。避免将其他环境的 site-packages 加入 PYTHONPATH。编译器、显卡驱动、Vulkan ICD、X11 和 CUDA 工具包等系统依赖需要单独安装。在服务器直接下载大型资产，并将缓存放在持久存储上。各指南分别列出训练数据集、仿真资产和模型权重的要求。

## 协议配置与本地路径

内置 profile 可能引用原容器的路径，例如 `/app`、`/assets` 和 `/workspace`。将其复制到 `local-profiles/`，修改副本后将绝对文件名传给 `--profile`。JSON 不会展开 shell 变量或 `~`。确保仿真进程或容器可以访问这些路径，并记录实际资产版本、任务子集以及对 seed 和 horizon 的改动。

三类配置分别负责以下内容。

- Profile 定义任务、episode 数、种子、仿真观测、步数上限和成功协议。
- 后端 JSON 向模型的 `create(**params)` 传入 checkpoint、RNG 等参数。
- CLI 执行参数控制设备、Worker、batch 大小、合批等待时间和调度方式。

## 核验运行结果

先用一个 Worker 和一个 episode 运行对应 benchmark 的假动作命令，并使用新的输出目录。命令成功启动或返回零成功率都不足以确认安装正确。

1. 查看 `protocol.json` 和 `episode-plan.json` 中的任务、原始索引、episode 种子与总数。
2. 检查 `runs/evaluation/status.json`、`summary.json` 和各个 `episode.json`，确认没有基础设施错误，步数按预期增加且最终结果已记录。
3. 检查观测与图像产物的维度、状态是否为有限数值，以及帧是否非空、非全黑。用能让机器人明显运动的动作对比 reset 后与后续帧。空动作可能让图像保持静止。
4. 分别核对原生成功信号与终止条件。假动作通常无法完成任务，仅凭其结果无法验证策略准确率或所有任务的成功路径。
5. 验证合批时，使用多个 Worker 和声明 `supports_batch=True` 的后端重复测试，核对请求与 session 的对应关系以及 episode 的完整计数。

```bash
find runs -name episode.json -o -name summary.json -o -name status.json
```

结果包括逐 episode、逐任务、benchmark 自定义维度及总体成功率。失败尝试会保留，统计时以最终汇总为准，同一 episode 的多个 attempt 只计一次。保存生成的 Graph、Module 配置以及协议元数据。使用子集或缩短 horizon 时，应明确标记评测范围。

## 故障排查

| 现象 | 检查项 |
| --- | --- |
| reset 前导入失败 | 仿真解释器、固定版本的依赖、`pip check`、原生共享库 |
| 缺少 XML、网格、任务或状态 | 资产是否完整、绝对根路径、解压后的目录层级、Git LFS 文件是否已下载为实际内容 |
| EGL 错误或选错 GPU | 检查 Worker 设备绑定和驱动可见性，避免用全局 EGL 设备覆盖全部 Worker |
| Vulkan 初始化失败 | 核对与 SAPIEN 兼容的 ICD 和容器图形能力，仅有 CUDA 可见性不足以验证渲染 |
| RLBench 图像全黑 | 检查 X11/GLX 和 Qt xcb。无显示器的机器应使用 Xvfb，避免 Qt offscreen |
| 首个 episode 较慢 | 检查资产下载、着色器或 JAX 编译、模型预热。完成准备后再增加并发 |
| 假动作成功率为零 | 空策略下通常如此。检查步数、图像和错误，模型准确率需要另行评估 |

## 已验证范围

[原生一致性报告](../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)覆盖 18 个非 Isaac 仿真环境的选定任务。[统一入口验证](../../docs/changes/unified-benchmark-entry-2026-09-29.zh-CN.md)记录模型与运行时迁移，[Kinetix 实时验证](../../docs/changes/kinetix-live-unified-2026-09-29.zh-CN.md)记录实时 Graph 行为。这些检查只覆盖指定场景，全任务准确率和全新环境安装需另行验证。

BEHAVIOR-1K 和 RoboDojo 的实际渲染仍需兼容的 RTX 硬件和获授权的资产。单元与配置测试只覆盖软件接口。此次文档更新不会停止、重启或热更新正在进行的评测。
