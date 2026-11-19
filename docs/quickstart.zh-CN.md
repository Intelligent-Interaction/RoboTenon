# 快速上手：从安装到第一批结果

[English](quickstart.md) · [**简体中文**](quickstart.zh-CN.md)

这篇文档带你走完 RoboTenon 最小闭环：

```text
安装 → 跑一个 Fake 任务 → 跑一批 CPU 实验 → 找到结果文件
```

全程不需要 GPU、模型权重、仿真器或机器人。完成后，你会知道框架能不能用，以及下一步该去哪里。

## 开始前

你需要：

- Python 3.10 或更高版本；
- Linux 或 macOS；
- 一个终端；
- 当前目录是仓库根目录，能看到 `pyproject.toml`、`robotenon/` 和 `configs/`。

不确定当前目录是否正确，可以执行：

```bash
pwd
ls pyproject.toml robotenon configs
python3 --version
```

## 第 1 步：安装

创建一个独立的 Python 环境，避免和机器上其他项目的依赖混在一起：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

确认导入的是当前仓库里的代码：

```bash
python -c 'import sys, robotenon; print(sys.executable); print(robotenon.__file__)'
```

第一行应该指向 `.venv`，第二行应该指向当前仓库中的 `robotenon/__init__.py`。

> 以后重新打开终端时，只需要再次执行 `source .venv/bin/activate`，不用每次重装。

核心安装只包含 RoboTenon 本身和开发工具，不包含 RoboCasa、MuJoCo、模型权重或机器人 SDK。需要录制视频时，再安装 `python -m pip install -e '.[dev,video]'`。

## 第 2 步：跑第一个任务

```bash
mkdir -p runs/tutorial
Tenon run-fake --config configs/config.json \
  --output runs/tutorial | tee runs/tutorial/result.json
```

输出会比下面多一些字段，先只看这两个：

```json
{
  "success": true,
  "workflow_status": "completed"
}
```

看到这两个值，说明安装、配置加载和核心工作流都正常。结果同时保存到了 `runs/tutorial/result.json`。

如果命令失败，不要先装 GPU 或仿真依赖；这个例子根本用不到它们。直接按[常见问题的 4 项检查](troubleshooting.zh-CN.md#先做这-4-项检查)排查。

## 刚才发生了什么

`run-fake` 是一场“消防演习”，不是机器人能力测试。它做了这些事：

1. 读取 `configs/config.json`；
2. 按配置加载内置组件；
3. 创建一个不会碰真实设备的 Fake 环境；
4. 按 Graph 执行几个步骤；
5. 打印结果并释放资源。

几个输出字段的含义：

| 字段 | 怎么理解 |
| --- | --- |
| `success` | 这次 Fake 检查是否通过 |
| `workflow_status` | 流程是否完整走到结束 |
| `session_id` | 这次环境会话的编号 |
| `steps` | Graph 走了多少步，不等于机器人动作次数 |
| `modules` | 这次实际加载了哪些业务模块 |

`--output runs/tutorial` 是 Fake 环境的工作目录；真正把终端 JSON 保存成文件的是 `tee`。这条命令不会生成正式 experiment、仿真视频或 Portal 实验记录。

更多名词见[术语表](glossary.zh-CN.md)。

## 第 3 步：先检查，再运行正式 CPU 示例

Fake 冒烟通过后，用教程配置做一次正式实验。先做预检：

```bash
Tenon run --preflight \
  --config docs/tutorials/examples/cpu-demo.json \
  --output-dir data/tutorial/quickstart
```

返回 JSON 中的 `ok` 应为 `true`。预检的意思是“配置、目录和本地资源看起来可以启动”，不是“真实模型和机器人已经验收”。

然后运行 3 个很小的 CPU 任务：

```bash
Tenon run --batch \
  --config docs/tutorials/examples/cpu-demo.json \
  --run-id quickstart \
  --output-dir data/tutorial/quickstart \
  --save-traces
```

检查汇总结果：

```bash
python - <<'PY'
import json
from pathlib import Path

path = Path("data/tutorial/quickstart/quickstart/summary.json")
summary = json.loads(path.read_text())
for key in ("num_episodes", "num_completed", "successes", "num_errors"):
    print(f"{key}: {summary[key]}")
PY
```

预期输出：

```text
num_episodes: 3
num_completed: 3
successes: 3
num_errors: 0
```

这时你已经走完了一个完整实验：任务被枚举、Worker 执行了 3 个 episode、结果和 Trace 被保存到磁盘。再次练习时，把 `--run-id` 换成 `quickstart-02`，不要覆盖旧结果。

## 第 4 步：选择你的使用方式

三种入口共用同一套配置、运行时和结果格式，可以混合使用。

### 在 Python 代码里调用

适合研究脚本、Notebook 后端、服务或自己的调度程序：

```python
import asyncio

from robotenon.runtime import EpisodeRequest, run_episode


async def main() -> None:
    result = await run_episode(
        EpisodeRequest(
            config="docs/tutorials/examples/cpu-demo.json",
            output="data/tutorial/python/episode.json",
            trace_dir="data/tutorial/python/trace",
        )
    )
    print(result["graph_status"], result["task_success"])


if __name__ == "__main__":
    asyncio.run(main())
```

保存为 `run_python.py`，执行 `python run_python.py`，预期看到 `completed True`。这个配置使用多进程后端，所以要保存成文件运行，并保留 `if __name__ == "__main__"`；不要直接粘进 `python - <<'PY'` 这类标准输入脚本。继续读[Python API 教程](tutorials/python-api.zh-CN.md)。

### 在终端运行

适合服务器、自动化脚本和批量评测。刚才的 CPU 示例就是正式 CLI 入口，继续读[命令行实验教程](tutorials/cli-experiments.zh-CN.md)。

### 在网页里运行

适合手动提交实验、看进度、轨迹和视频：

```bash
Tenon web \
  --host 127.0.0.1 --port 8765 \
  --project-root . --config-root docs/tutorials/examples \
  --experiment-output-root data/tutorial/portal \
  --run-root data/tutorial/portal \
  --no-auto-grafana
```

打开 <http://127.0.0.1:8765/>，选择 `cpu-demo.json`。继续读 [Web Portal 教程](tutorials/03-portal.zh-CN.md)。

## 第 5 步：换成真实环境

RoboTenon 把外部仿真器、模型和设备做成 extension。先安装对应 extension，再让配置显式加载它。

| 目标 | 下一篇 |
| --- | --- |
| 了解 extension 怎么安装和加载 | [使用 extensions](tutorials/extensions.zh-CN.md) |
| 跑 RoboCasa 和模型 | [首次仿真](tutorials/02-first-simulation.zh-CN.md) |
| 准备场景资产和模型权重 | [数据与模型下载](tutorials/download-data.zh-CN.md) |
| 接入物理机器人 | [真机部署](tutorials/real-robot.zh-CN.md) |
| 扩展到多 Worker / GPU | [8 卡服务器部署](tutorials/server-8gpu.zh-CN.md) |

安装 extension 并不等于依赖已经齐全。仿真器、GPU 版 PyTorch、模型权重和设备 SDK 通常要按各 extension 的 README 单独准备。

## 你现在应该记住什么

- 先跑 Fake，再上仿真或真机。
- `completed` 表示流程走完，`task_success` 才表示任务成功。
- 配置决定加载什么；Python、CLI 和 Portal 只是不同入口。
- 每次正式运行使用新的 `run_id`，保留旧结果方便对比和排查。
- 不知道某个词时查[术语表](glossary.zh-CN.md)，报错时查[常见问题](troubleshooting.zh-CN.md)。

想继续系统学习，按[教程路线](tutorials/README.zh-CN.md)往下走；只想查参数，直接去[参考手册](README.zh-CN.md#参考手册)。

## 从旧版本迁移

在 Worker、模型和仿真环境中重新安装框架及使用的扩展。将 Python 导入、插件工厂和配置中的包前缀从 `robosys` 改为 `robotenon`，环境变量前缀从 `ROBOSYS_` 改为 `ROBOTENON_`。运行命令使用 `Tenon`。重新启动服务，使运行进程和配置使用同一版本。
