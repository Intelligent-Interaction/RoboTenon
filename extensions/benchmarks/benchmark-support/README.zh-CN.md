# 共享评测运行时

[English](README.md) · [**简体中文**](README.zh-CN.md)

[用户入口](../README.zh-CN.md) · [快速开始](../QUICKSTART.zh-CN.md) · [评测手册](../EVALUATION.zh-CN.md) · [模型接口](../models/README.zh-CN.md)

此目录提供统一评测命令和运行时适配。运行评测从上方用户文档开始；下面面向需要修改实现的开发者。

## 代码职责

| 文件 | 职责 |
| --- | --- |
| `evaluate.py` | 参数解析、协议校验、模型服务与仿真进程生命周期 |
| `backend_loader.py` | 文件/module 后端解析、独立命名空间、SPEC 校验 |
| `runtime.py` | 同步模型 history、action chunk 和 session 生命周期 |
| `live_runtime.py` | 实时消息与最新动作选择 |
| `configure.py` | 应用、Graph 和 Module 配置生成 |
| `integration.py` | Environment、Body、TaskAdapter 及 native step 边界 |
| `scheduling.py` | 独立 RNG 打乱稳定 episode 清单 |
| `static_execution.py` | 可选固定 Worker 槽位分配 |
| `execution.py` | native_sync 及 Worker native 实例生命周期支持 |
| `codec.py` | observation 中数组和图像的传输 |
| `dimensions.py`、`results.py`、`recording.py` | 指标维度、聚合与记录接口 |

默认统一入口使用进程 Worker 与独立模型服务。底层 `configure`、旧 policy 适配和 `native_sync` 是开发接口，不是另一套推荐的用户接入路径。新模型遵循 `models/README.md` 的 batch 接口，单样本为 B=1。

## 扩展边界

benchmark 负责任务、seed、horizon、success、原生 observation/action；模型负责 checkpoint、预处理、RNG 和 action 转换；runtime 负责历史、chunk、调度及进程生命周期。同步和实时协议的差异必须显式配置，不应在模型中隐式改变仿真时钟。

兼容模块 `robotenon.benchmarks` 和 extension 内旧 `vla.py` 不作为新增模型的存放位置。上游来源记录与许可证保留，协议对齐测试不能代替全部任务的物理或成功率验证。

## 测试与探针

在已安装框架和相关 extension 的测试环境中运行：

```bash
python -m pytest tests/test_native_graph_configs.py tests/test_unified_benchmark_backend.py tests/test_dynamic_batching.py tests/test_live_benchmark.py
```

独立仿真探针 `python -m robotenon_benchmark_support.smoke --help` 支持脚本化 action、`--save-frames` 和 `--render gpu|cpu`。检查图像有效性、状态和逐步变化；无动作导致的静止不等于渲染故障。软件渲染仍需对应驱动。

## 验证记录

- [原生动作/图像差分](../../../docs/changes/native-action-parity-2026-09-28.zh-CN.md)：18个非Isaac环境的选定任务，非全套跑分认证。
- [统一入口和模型迁移](../../../docs/changes/unified-benchmark-entry-2026-09-29.zh-CN.md)。
- [Kinetix 实时模式](../../../docs/changes/kinetix-live-unified-2026-09-29.zh-CN.md)。
- [实时状态有界窗口](../../../docs/changes/bounded-live-status-2026-09-29.zh-CN.md)：最近256条记录、普通状态每秒刷新、错误/终止立即写入。

这些记录面向开发追溯；安装资产和运行命令以当前用户文档为准。

## 原生系统 Graph 接入

统一 evaluate 支持 `--system-config` 和 `--execution-config`，继续使用框架配置加载器、SharedBatchServices 和 Tool 生命周期。system_config.py 只组合配置并保护 benchmark 的环境/协议；model_tool.py 将原生观测送至非动作模型服务。见 [系统接入手册](../SYSTEMS.zh-CN.md)。
