# Benchmark 运行依赖与成功率统计

[English](benchmark-requirements-and-results.md) · [**简体中文**](benchmark-requirements-and-results.zh-CN.md)

## 运行依赖

每个独立 benchmark 都维护自己的 `extensions/benchmarks/<benchmark>/requirements.txt`。在专用仿真环境中安装这份依赖，再安装 RoboTenon、公共评测支持包和对应的扩展。

```sh
python -m pip install -r extensions/benchmarks/libero/requirements.txt
python -m pip install -e . -e extensions/benchmarks/benchmark-support -e extensions/benchmarks/libero
```

这些文件按已核对的上游环境固定 Python 包和源码仓库的版本。系统库、需要许可的仿真器、数据集和资产需要另外准备。具体前提见每个 requirements 文件的开头注释，以及 `protocol-reference/upstream-environment.txt`。依赖不兼容的仿真器应安装在独立环境中。例如，BEHAVIOR-1K 和 RoboDojo 需要不同版本的 Isaac Sim。

请直接在执行评测的服务器上下载资产。无需经过开发工作站中转，也不要将资产归档提交到 Git。

## 成功率

每个 episode 的输出都包含 `native_dimensions`。结果聚合器提供下列字段。

- `overall` 汇总已终止 episode 的成功数、错误数和成功率。
- `by_task` 提供每个原生任务的成功率。
- `by_dimension` 按扩展声明的维度及其取值分别计算成功率。
- 顶层保留旧版 `mean_success` 和 `tasks` 字段以兼容已有调用方。

执行出错和任务失败都计为未成功。同一稳定 TaskCase 标识对应多次尝试时，只统计最后一次。等待中或正在运行的 episode 暂不计入统计。如果已完成数仍少于预期数，摘要标记为 `partial`。

```sh
python -m robotenon_benchmark_support.results \
  runs/my-eval/**/episode_*.json \
  --expected-episodes 100 \
  --output runs/my-eval/summary.json
```

以下示例展示了输出结构。

```json
{
  "overall": {"num_episodes": 100, "num_successes": 63, "success_rate": 0.63},
  "by_dimension": {
    "suite": [
      {"value": "seen", "num_episodes": 50, "num_successes": 36, "success_rate": 0.72},
      {"value": "unseen", "num_episodes": 50, "num_successes": 27, "success_rate": 0.54}
    ]
  }
}
```

## 各 benchmark 声明的维度

| Benchmark | 成功率细分维度 |
| --- | --- |
| BEHAVIOR-1K | 套件、任务 |
| CALVIN | 评测序列、初始条件 |
| DuoBench | 任务 |
| Kinetix | 关卡 |
| LIBERO | 套件、任务 |
| LIBERO-Mem | 套件、任务 |
| LIBERO-Plus | 套件、类别、难度、任务 |
| LIBERO-Pro | 套件、任务 |
| ManiSkill2 | 环境 |
| MIKASA | 任务 |
| MolmoSpaces | 任务类型、任务 |
| RLBench | 任务 |
| RoboCasa | 任务 |
| RoboCasa365 | 套件、任务 |
| RoboCerebra | 任务类型、案例 |
| RoboDojo | 套件、任务 |
| RoboMME | 环境 |
| RoboTwin | 套件、任务 |
| Simpler | 任务、环境、场景、机器人 |
| VLABench | 任务、机器人 |

聚合器仅输出 episode 中实际存在的维度。如果 profile 没有提供某项可选元数据，对应细分不会出现在结果中。
