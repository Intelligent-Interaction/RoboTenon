# 首次 benchmark 仿真

[English](02-first-simulation.md) · [**简体中文**](02-first-simulation.zh-CN.md)

本教程用 RoboCasa365 的统一 extension 入口验证仿真、图像、action 和终止信号。

## 1. 安装

在 RoboCasa 仿真环境中执行：

```bash
python -m pip install -e . -e extensions/benchmarks/benchmark-support -e extensions/benchmarks/robocasa365
python -m pip install -r extensions/benchmarks/robocasa365/requirements.txt
```

按 extension README 准备 RoboCasa365 资产。独立 fake 后端位于
`extensions/benchmarks/models/fake/vla.py`，用于验证仿真能否推进。

## 2. 运行 smoke profile

```bash
Tenon evaluate --benchmark robocasa365 \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --backend-config ./extensions/benchmarks/models/fake/configs/robocasa365.json \
  --sim-python /path/to/robocasa/python \
  --profile extensions/benchmarks/robocasa365/configs/smoke.json \
  --output runs/robocasa365-smoke
```

命令会生成 `config.json`、`config_graphs.json` 和 `config_modules.json`，然后
运行 `reset → policy → execute → observe → terminal`。检查 episode JSON 的
`graph_status`、`outputs.env_steps`、`outputs.native_metrics` 和录制图像。fake
action 不能用于判断模型准确率。

## 3. 接入模型

在独立模型目录中实现 `vla.py`，声明 `SPEC`，提供 `create()`、`open_session`、
`act_batch`、`close_session` 和 `close`。通过 `--backend` 指定文件路径。
不要在模型代码中调用环境 reset/step 或自行生成 success。再次执行同一命令
即可使用新模型。

Xiaomi-Robotics-1 的正式 target50 × 50 命令见
[extension README](../../extensions/benchmarks/robocasa365/README.zh-CN.md)。
