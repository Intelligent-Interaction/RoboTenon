# 使用 benchmark extensions

[English](extensions.md) · [**简体中文**](extensions.zh-CN.md)

每个 benchmark 是独立 Python 包。它提供仿真协议、Environment、Body 和
TaskAdapter，但不修改 RoboTenon 核心。所有 benchmark 使用同一公开结构。

## 1. 选择并安装

以 LIBERO 为例：

```bash
python -m pip install -e . -e extensions/benchmarks/benchmark-support -e extensions/benchmarks/libero
python -m pip install -r extensions/benchmarks/libero/requirements.txt
```

仿真器、GPU 版框架、资产、数据集和设备 SDK 由各 extension 的 README
说明。存在依赖冲突的仿真器应使用独立环境。Worker 使用的 Python 必须能
导入核心、`benchmark-support` 和对应 extension。

## 2. 接入模型

模型入口只有：

```text
extensions/benchmarks/<benchmark>/src/robotenon_<benchmark>/vla.py
```

保留 `create()`，实现 `start_episode()`、`act()`、`end_episode()` 和可选的
`close()`。默认 fake VLA 只用于验证接口和仿真推进。

## 3. 运行

选择直接位于 extension `configs/` 下的 profile：

```bash
Tenon evaluate --benchmark libero \
  --backend /path/to/my_model/vla.py --model-python /path/to/model/python \
  --sim-python /path/to/libero/python \
  --profile extensions/benchmarks/libero/configs/spatial.json \
  --output runs/libero
```

`evaluate.py` 会在输出目录生成完整 master、Graph 和 Module 配置，再通过
标准实验 runner 执行。无需维护 `native_test.json`、Graph 清单或 Portal
镜像配置。

结果位于：

```text
runs/libero/config.json
runs/libero/config_graphs.json
runs/libero/config_modules.json
runs/libero/runs/<run-id>/summary.json
```

summary 同时包含总体成功率、逐任务成功率、各维度成功率、错误数和完整性
统计。正式报告应使用 README 标注的上游 profile；短 smoke profile 只验证
安装和 action 流。

## 4. 批量推理

支持 batch 的 `vla.py` 还可提供 `create_batch()`。使用共享配置生成器设置
仿真 Worker、模型副本、batch size 和等待窗口：

```bash
python -m robotenon_benchmark_support.configure \
  --profile extensions/benchmarks/libero/configs/spatial.json \
  --batch-interface batch --inference-mode batch \
  --workers 8 --worker-devices 0,0,1,1,2,2,3,3 \
  --model-devices 0,1,2,3 --batch-size 8 --batch-wait-ms 50 \
  --output runs/libero-batch/config.json
```

动态 batch 改变模型调度，仿真任务仍由各自 Worker 顺序推进。模型历史和
action 缓存必须按 session ID 隔离。

## 5. 特殊运行时

- BEHAVIOR-1K 和 RoboDojo 使用不同 Isaac Sim 栈，需要支持的 RTX 主机和
  已授权资产。
- RLBench 需要匹配的 CoppeliaSim 安装和适用许可。
- Kinetix realtime profile 需要专用 live Graph；同步 evaluator 会明确拒绝。
- RoboCasa365 的 Xiaomi accuracy runner 和吞吐 speed tool 是两个入口。

完整协议见 [Graph 与 VLA](../native-benchmark-graph.zh-CN.md)，profile 清单见
[benchmark catalog](../benchmark-catalog.json)，每个 extension 的安装和命令
以该目录 README 为准。
