# <img src="robotenon/web/static/tenon.svg" width="40" height="40" align="middle" alt=""> RoboTenon

[English](README.md) · [**简体中文**](README.zh-CN.md)

**用于执行和评测机器人策略的模块化运行时。**

Tenon 指榫卯结构中的榫头。RoboTenon 的工作流图通过明确的接口连接感知、策略、执行和评测，各组件可以独立替换。同一张图可以通过 Environment 和 Body 适配器接入仿真器或真实机器人。Python 包名为 `robotenon`，命令行工具为 `Tenon`。

## 运行第一个实验

在 Linux 或 macOS 的仓库根目录使用 Python 3.10 或更高版本，运行内置的 CPU 示例。示例使用测试环境，无需模型权重或机器人硬件。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
Tenon run-fake --config configs/config.json --output runs/first-run
```

命令会打印 JSON 结果。成功运行时，输出包含 `"success": true` 和 `"workflow_status": "completed"`。配置验证方法和输出说明见[快速上手](docs/quickstart.zh-CN.md)。

## 系统架构

- **Graph 与 Module** 定义执行顺序、条件分支和步骤之间传递的数据。Module 可以通过 Tool 调用模型和其他服务。
- **Environment 与 Body 适配器** 提供观测、执行动作并返回结果。真机部署需接入对应设备的观测源与驱动。
- **Worker** 执行 episode 并管理资源。多个 Worker 可以共享模型服务，由服务对推理请求进行合批。
- **Benchmark 扩展** 定义任务、随机种子、动作格式和成功判据。在相同协议下可以评测单一策略或完整系统。

## 文档导航

| 主题 | 指南 |
| --- | --- |
| 安装与首次实验 | [快速上手](docs/quickstart.zh-CN.md) · [Python API](docs/tutorials/python-api.zh-CN.md) · [命令行](docs/tutorials/cli-experiments.zh-CN.md) |
| 构建系统 | [系统架构](docs/architecture.zh-CN.md) · [Graph 工作流](docs/workflow.zh-CN.md) · [插件开发](docs/plugin_development.zh-CN.md) |
| 接入仿真器或设备 | [首次仿真](docs/tutorials/02-first-simulation.zh-CN.md) · [真机部署](docs/tutorials/real-robot.zh-CN.md) |
| 评测策略 | [评测指南](docs/benchmarks.zh-CN.md) · [支持的环境](extensions/benchmarks/README.zh-CN.md) · [参考评测结果](docs/reference-runs.zh-CN.md) |
| 检查运行情况 | [Web Portal](docs/tutorials/03-portal.zh-CN.md) · [结果与轨迹](docs/tutorials/view-results.zh-CN.md) · [常见问题](docs/troubleshooting.zh-CN.md) |

完整的参考手册和开发指南见[文档索引](docs/README.zh-CN.md)。
