# Python API 使用教程

[English](python-api.md) · [**简体中文**](python-api.zh-CN.md)

本教程说明如何在 Python 程序中 import `robotenon`，运行单个 episode 或批量实验。Python API 与 CLI、Web Portal 使用同一套配置加载、组件注册、Worker 和产物格式；适合嵌入研究代码、Notebook 后端、自定义服务或调度程序。

## 1. 安装与导入

在仓库根目录安装后检查导入：

```bash
python -m pip install -e '.[dev]'
python -c 'import robotenon; print(robotenon.__version__)'
```

高层运行入口位于 `robotenon.runtime`：

```python
from robotenon.runtime import (
    EpisodeRequest,
    ExperimentRequest,
    run_episode,
    run_experiment,
)
```

`run_episode()` 和 `run_experiment()` 都是异步函数。已在异步应用中时直接 `await`；普通脚本使用 `asyncio.run()`。

## 2. 运行单个 episode

创建 `run_python.py`：

```python
import asyncio
import json

from robotenon.runtime import EpisodeRequest, run_episode


async def main() -> None:
    request = EpisodeRequest(
        config="docs/tutorials/examples/cpu-demo.json",
        task_id="cpu-demo",
        env_id="fake",
        seed=7,
        output="data/tutorial/python/episode.json",
        trace_dir="data/tutorial/python/trace",
        metrics_dir="data/tutorial/python/metrics",
    )
    result = await run_episode(request)
    print(
        json.dumps(
            {
                "graph_status": result["graph_status"],
                "task_success": result["task_success"],
                "output_path": result["output_path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
```

在仓库根目录执行：

```bash
python run_python.py
```

本例预期 `graph_status` 为 `completed`、`task_success` 为 `true`。配置使用 `process` 后端，因此脚本需要 `if __name__ == "__main__"` 保护；不要把这个例子直接放在会被子进程重新导入的模块顶层。

`EpisodeRequest` 中的非 `None` 字段会覆盖配置值。常用字段包括 `task_id`、`env_id`、`seed`、`horizon`、`model_path`、`graph_id`、`output`、`video_path`、`trace_dir` 和 `environment_parameters`。返回值是一个字典；实际结果文件位置以 `output_path` 为准，因为不同执行后端的 attempt 目录布局不同。

## 3. 运行批量实验

`ExperimentRequest` 会按配置中的 TaskAdapter 枚举任务，对应 CLI 的 `--batch`：

```python
import asyncio
import json

from robotenon.runtime import ExperimentRequest, run_experiment


async def main() -> None:
    result = await run_experiment(
        ExperimentRequest(
            config="docs/tutorials/examples/cpu-demo.json",
            output_dir="data/tutorial/python-batch",
            run_id="cpu-demo-api",
            save_traces=True,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
```

这份 CPU 配置会运行 3 次。摘要文件位于 `data/tutorial/python-batch/cpu-demo-api/summary.json`，应显示 `num_completed: 3`、`successes: 3` 和 `num_errors: 0`。每次重试时使用新的 `run_id`，避免与已有实验产物冲突。

## 4. 参数覆盖

单任务可直接使用 `EpisodeRequest` 字段。批量实验使用 `task_adapter_overrides` 按配置实例 ID 或注册名选择 TaskAdapter：

```python
request = ExperimentRequest(
    config="path/to/config.json",
    task_adapter_overrides={
        "benchmark": {"max_tasks": 2, "num_trials": 1}
    },
    environment_parameters={"settle_steps": 20},
    output_dir="data/runs",
)
```

对象字段递归合并，数组和标量整体替换。显式指定不存在的 TaskAdapter 会报错。环境专用选项通过 `environment_parameters` 传入，不要为新环境修改通用 Runner 的字段。

## 5. 资源与错误处理

- 等待 `run_episode()` / `run_experiment()` 返回时，Worker、环境会话、可观测性服务和资源锁已按正常路径清理。
- 不要在同一个事件循环中同步嵌套 `asyncio.run()`；在 FastAPI、Jupyter 等已有事件循环的环境直接 `await`。
- `graph_status == "completed"` 只表示流程结束；真实业务成败还要检查 `task_success`、`reason` 和 `outputs`。
- 调用方负责记录并处理异常。不要用 `dry_run=True` 代替 Fake 环境安全演练；`dry_run` 不会自动替换环境或保证真机不动作。

产物字段和 Trace 解读见[查看实验结果](view-results.zh-CN.md)；完整 CLI 对照见[命令行运行实验](cli-experiments.zh-CN.md)。
