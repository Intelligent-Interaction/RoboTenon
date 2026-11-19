# Benchmark 使用指南

[English](benchmarks.md) · [**简体中文**](benchmarks.zh-CN.md)

用同一个入口选择仿真环境、评测协议和模型，并查看成功率。所有命令在仓库根目录执行；示例中的 `/opt`、`/data` 路径需要替换为本机实际路径。

已有普通 Graph 的使用经验不是前提。统一入口会自动生成 Graph、Module、Tool 和仿真配置。需要评测规划器 + VLA + verifier 时，使用同一入口的[系统配置](../extensions/benchmarks/SYSTEMS.zh-CN.md)。

多卡配置示例见 [Xiaomi + RoboCasa365](tutorials/benchmark-h100-xiaomi.zh-CN.md)、
[Xiaomi + VLABench](tutorials/benchmark-h100-xiaomi-vlabench.zh-CN.md)和
[Xiaomi Robotics 0 + LIBERO](tutorials/benchmark-h100-xiaomi-libero.zh-CN.md)、
[Xiaomi Robotics 0 + LIBERO-Plus](tutorials/benchmark-h100-xiaomi-libero-plus.zh-CN.md)：包含硬件容量参考、并行参数、模型配置、实测结果和排错方法。

## 1. 选择环境、协议和模型

| 选择 | 参数 | 含义 |
| --- | --- | --- |
| 环境 | `--benchmark robocasa365` | 安装哪个仿真栈，采用哪种 action/observation |
| 协议 | `--profile xiaomi_official` | 任务集、episode 数、seed、horizon 和观测规格；也可传 JSON 路径 |
| 模型 | `--backend /data/models/my-model/vla.py` | 独立模型工厂；也支持 `my_model.vla:create` |
| 模型配置 | `--backend-config /data/models/my-model/config.json` | 原样传给 `create(**params)` 的 checkpoint、精度、RNG 等参数 |
| 执行资源 | GPU、Worker、batch 等选项 | 控制如何执行，不替代评测协议 |

[Benchmark 索引](../extensions/benchmarks/README.zh-CN.md#benchmark-索引)列出全部 20 个环境及其 README、SETUP 和 profile。每个环境的安装、资产、许可证和验证范围以其 SETUP 为准。BEHAVIOR-1K、RoboDojo 需要对应 Isaac/RTX 条件，尚未完成真实渲染验证。

模型后端必须明确声明支持的 benchmark，不能只更换 benchmark 名来适配另一种机器人。`models/xiaomi` 面向 RoboCasa365，`models/xiaomi-robotics0` 面向 LIBERO、LIBERO-Plus 和 LIBERO-Pro。LIBERO-Plus 的 `all-0` 至 `all-3` 是四个 suite，可通过 `--profiles all-0 all-1 all-2 all-3` 合并调度，见 [LIBERO-Plus](../extensions/benchmarks/libero_plus/README.zh-CN.md)。

## 2. 安装并下载资产

建议模型和仿真使用两个独立 Python 环境。不同 benchmark 的仿真依赖也分别安装，避免 MuJoCo、SAPIEN、Isaac、NumPy 和 torch 版本冲突。两个环境位于同一服务器，共享可见的文件系统。

```bash
export MODEL_PY=/opt/envs/model/bin/python
export SIM_PY=/opt/envs/robocasa365/bin/python
export BENCHMARK=robocasa365

"$MODEL_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support
# 先按所选 benchmark 的 SETUP 完成系统库、仿真依赖及构建顺序，再安装适配包：
"$SIM_PY" -m pip install -e . -e extensions/benchmarks/benchmark-support \
  -e "extensions/benchmarks/$BENCHMARK"
"$MODEL_PY" -m robotenon_benchmark_support.evaluate --help
```

这些命令不会下载仿真资产或模型权重。按 [共享安装说明](../extensions/benchmarks/SETUP.zh-CN.md)及各环境 SETUP，在实际运行服务器下载。模型依赖只安装到模型环境，不通过跨环境 `site-packages` 注入解决冲突。配置中的资产路径使用绝对路径，JSON 不展开 `$VARIABLE` 或 `~`。

## 3. 先验证一个 episode

RoboCasa365 安装就绪后运行：

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile smoke \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --backend-config ./extensions/benchmarks/models/fake/configs/robocasa365.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/robocasa365-fake-smoke
```

fake 模型也会运行真实仿真，需要资产和渲染环境。检查 episode 无错误、步数推进、状态与图像有效；零动作可能不产生画面变化，验证运动应选用环境支持的非零动作。fake 成功率为零不等于安装失败。

随后换真模型的 `--backend` 和 `--backend-config`，保留单 Worker 和任务子集验证输入输出。自定义模型只需实现独立 `vla.py` 的 `SPEC`、`create()` 及 session/batch 接口，详见[模型接口](../extensions/benchmarks/models/README.zh-CN.md)。不需要修改 evaluate、Graph 或 benchmark。

## 4. 扩大到多卡与正式协议

以下是 Xiaomi + RoboCasa365 完整 target50 × 50 示例。先按照 [Xiaomi 后端说明](../extensions/benchmarks/models/xiaomi/README.zh-CN.md)安装模型依赖，将其配置复制到 `/data/configs/xiaomi.json`，填入已下载 checkpoint 的绝对 `model_path` 和明确的 `rng_mode`。

```bash
# 限制各子进程的 CPU 数值库线程，降低大量 Worker 的过度调度。
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config /data/configs/xiaomi.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 --workers-per-device 16 \
  --batch-size 8 --wait-ms 50 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --startup-parallelism 16 --recycle-after-tasks 5 \
  --output runs/robocasa365-xiaomi-full
```

这会启动 **8 个模型副本、128 个仿真 Worker**。每卡 28 个 Worker 对应 224 个 Worker；192 个对应每卡 24 个。示例并发量需要按机器内存、CPU、显存和渲染能力调整，不是所有 benchmark 的推荐值。上述环境变量限制常用线程池，不是进程总线程数的硬上限。

正式运行去掉 smoke 的任务/episode 覆盖，使用新的 output 目录。RoboCasa365 的正式 profile 保留各任务原始 horizon，每步 success 后立即结束。其他环境继续使用自己的协议语义。

| 能力 | 使用方法与边界 |
| --- | --- |
| 动态 batch | `--batch-size 8 --wait-ms 50`；8 是上限，实际 batch 可为 1–8，50ms 不包括所有排队和推理延迟 |
| 多 GPU | 每个 device 一个常驻模型服务；Worker 固定连接对应服务，各服务独立组 batch |
| 同卡多个副本 | `--virtual-gpu-map` 配置逻辑槽位；增加副本占用额外显存，不提供硬件隔离，见[虚拟 GPU](../extensions/benchmarks/VIRTUAL_GPUS.zh-CN.md) |
| 全局 episode 队列 | 默认 shuffle，空闲 Worker 动态领取；不跨 GPU 汇总推理请求，也不迁移执行中的 episode |
| 固定调度 | `--scheduling-strategy static --no-shuffle` 用于对照 |
| 启动/reset 并发 | `--startup-parallelism` 控制同时初始化数量，不是总 Worker 数 |
| 进程回收 | `--recycle-after-tasks 5` 表示每 Worker 完成 5 次执行后重建，不是完成 5 种任务的全部 episodes；模型服务保持常驻 |
| 实时环境 | Kinetix `realtime` 自动使用 live Graph，环境与模型异步推进，见[Kinetix](../extensions/benchmarks/kinetix/README.zh-CN.md) |
| 完整系统 | `--system-config` 指定 Graph，`--execution-config` 指定各模型服务，见[系统评测](../extensions/benchmarks/SYSTEMS.zh-CN.md) |

可将执行参数保存到 `--execution-config` JSON，显式 CLI 参数覆盖该文件的默认值。完整选项及 EGL 绑定见[评测手册](../extensions/benchmarks/EVALUATION.zh-CN.md#8-完整-cli-参数表)。

Shuffle 使用独立调度 RNG，保留任务原始索引、episode 编号和环境 seed。**它不保证模型轨迹或最终成功率与调度无关**：Xiaomi 的 `official_stream` 使用模型服务全局 RNG，请求次序、副本数和 batch 都可能改变随机数分配；`session_deterministic` 用于隔离 session RNG 的性能对照，仍可能有 batch 浮点差异。

完整协议的吞吐示例：8 张 H100、224 个 Worker 完成 2,500 个 RoboCasa365 episode，用时 **106.70 分钟**，端到端 **23.43 episodes/分钟**，成功率 **57.56%**。详情与公开数据见[评测结果与并行效率](tutorials/benchmark-h100-xiaomi.zh-CN.md#评测结果与并行效率)。

LIBERO 示例：8 张 H100、288 个 Worker 完成 2,000 个 episode，用时 **20.59 分钟**，端到端 **97.15 episodes/分钟**，成功率 **97.15%**。任务级失败和复现注意事项见 [Xiaomi Robotics 0 + LIBERO](tutorials/benchmark-h100-xiaomi-libero.zh-CN.md)。

LIBERO-Plus 示例：8 张 H100、288 个 Worker 完成 10,030 个扰动案例，用时 **127.95 分钟**，端到端 **78.39 episodes/分钟**，成功率 **74.77%**。详情见 [Xiaomi Robotics 0 + LIBERO-Plus 全量评测](tutorials/benchmark-h100-xiaomi-libero-plus.zh-CN.md)。

## 5. 查看进度、速度和结果

| 文件，相对 output | 用途 |
| --- | --- |
| `protocol.json` | 原始/实际协议、覆盖项、后端、RNG、调度和执行参数 |
| `episode-plan.json` | 实际计划和稳定 episode 身份 |
| `config*.json` | 自动生成的应用、Graph、Module 配置 |
| `runs/evaluation/status.json` | 运行中的 summary 和活动状态 |
| `runs/evaluation/summary.json` | 最终总体、逐任务和维度统计 |
| `runs/evaluation/episodes.sqlite3` | episode 索引；查询完成数量与近期速度 |
| `batch-metrics/*.json` | 每个服务的真实 batch 分布、请求数、排队/推理耗时 |

`--run-id` 修改时，将下面路径中的 `evaluation` 替换为实际值。运行中 summary.json 可能未更新；用 status.json 或 episode 索引查询。下面的只读命令按最终 episode 记录统计，不重复计入 attempts：

```bash
export EVAL_OUTPUT=/data/runs/robocasa365-xiaomi-full
"$MODEL_PY" - <<'PY'
import datetime as dt
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

path = Path(os.environ['EVAL_OUTPUT']) / 'runs/evaluation/episodes.sqlite3'
with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
    rows = db.execute('SELECT updated_at, record_json FROM episodes').fetchall()
records = [(dt.datetime.fromisoformat(t.replace('Z', '+00:00')), json.loads(r))
           for t, r in rows]
counts = Counter(r.get('status', 'unknown') for _, r in records)
done = [(t, r) for t, r in records if r.get('status') == 'completed']
successes = sum(bool(r.get('task_success')) for _, r in done)
now = dt.datetime.now(dt.timezone.utc)
rates = {m: sum(0 <= (now-t).total_seconds() <= m*60 for t, _ in done) / m
         for m in (5, 10, 30)}
print('状态:', dict(counts), '计划记录:', len(records))
print('成功:', successes, '已完成部分成功率:', successes / len(done) if done else None)
print('最近 episodes/min:', rates)
PY
```

成功提前终止、失败跑满 horizon 时，早期已完成部分成功率通常偏高，即使清单已 shuffle 也会发生。最终按完整计划汇总，保留 errors/cancelled，不把运行错误当成普通任务失败或静默剔除。

估算剩余时间可用剩余 episode 数除以最近 10–30 分钟速度；冷启动、环境重建和末尾长 episode 会影响预测。batch 上限不等于实际大小，平均 batch 用所有服务 `requests` 总和除以 `batches` 总和；近期指标用两次快照差值。GPU 利用率是瞬时值，需要结合完成速度、错误、内存和队列判断。

## 6. 中断、性能和排查

当前统一入口没有自动 `--resume`。WorkerLost 的运行内重试与整次评测断点续跑是两件事。中断后保留原目录，明确补跑 episode 清单与去重规则；不在原 output 上覆盖重跑。

运行在服务器的 tmux 等持久会话中。先完成单 Worker 和单卡验证，再增加并发。批处理偏小时检查各模型服务请求速率；更多 Worker 或更长等待窗口并不一定更快。统一入口默认 `trace-mode=summary`、`history-cache=auto`，诊断记录窗口与模型观测历史互相独立。Xiaomi 的 4/2/16 历史配置没有相邻请求的历史重叠，auto 默认不启用增量缓存。

更多内容：[全部执行参数与故障边界](../extensions/benchmarks/EVALUATION.zh-CN.md)、[模型接口](../extensions/benchmarks/models/README.zh-CN.md)、[Graph 与生命周期](native-benchmark-graph.zh-CN.md)、[安装排查](../extensions/benchmarks/SETUP.zh-CN.md#故障排查)。
