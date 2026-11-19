# RoboTwin 2.0 全量评测运行手册

[English](RUNBOOK.md) · [**简体中文**](RUNBOOK.zh-CN.md)

本文整理 RoboTwin 2.0 全量评测的部署、启动、监控和排错注意事项。协议定义与基本用法见
[README](README.zh-CN.md)，安装与资产准备见 [SETUP](SETUP.zh-CN.md)，框架通用参数见
[统一评测手册](../EVALUATION.zh-CN.md)。

## 1. 先区分协议参数和性能参数

官方完整口径为：

- 50 个任务；
- `demo_clean`（Easy）和 `demo_randomized`（Hard）两种设置；
- 每个任务、每种设置各 100 个 episode；
- 合计 `50 × 2 × 100 = 10,000` 个 episode。

[`generate_official_profiles.py`](scripts/generate_official_profiles.py) 生成 100 个 profile，
每个 profile 通过 `test_num=100` 枚举 100 个 episode。正式运行前应在
`episode-plan.json` 或 `episodes.sqlite3` 中确认总数确实为 10,000。

以下参数属于协议的一部分，不能为了提速或提高分数随意修改：

- 任务集合和 Easy/Hard 配置；
- episode 数、seed 和 instruction 类型；
- 环境观测、动作语义和成功判定；
- 每个任务的 horizon。

任务 horizon 优先读取 RoboTwin 的
`env_cfg/task_config/_eval_step_limit.yml`。只有文件缺失或值无效时，适配器才回退到 400。
不要把所有任务强制设置成统一 horizon；否则结果不能再与官方口径直接比较。

以下参数是性能参数，可以在不改变任务协议的前提下调优，但必须随结果一起记录：

- `workers-per-gpu`；
- `batch-size` 和 `wait-ms`；
- `startup-parallelism` 和 `enumeration-parallelism`；
- `recycle-after-tasks`；
- 调度策略、超时和线程环境变量。

## 2. 推荐目录布局

代码、模型、资产和结果应相互独立。共享盘上的一种推荐布局如下：

```text
<shared-root>/
  models/pi0.5_robotwin2/
  profiles/robotwin2-official/
  sources/openpi/
  sources/robotwin-2.0/
  runs/
  logs/
```

当前适配器要求 RoboTwin checkout 可从 `/app/RoboTwin` 访问。若真实数据位于共享盘，可使用
符号链接，但重建节点后必须重新创建并检查：

```bash
test -d "$SHARED_ROOT/sources/robotwin-2.0/assets"
ln -sfn "$SHARED_ROOT/sources/robotwin-2.0" /app/RoboTwin
test -f /app/RoboTwin/env_cfg/task_config/_eval_step_limit.yml
```

不要把训练数据当作评测资产。全量仿真需要 RoboTwin 源码、embodiments、objects、背景纹理、
任务配置和机器人资源；缺少任意一类都可能只在特定任务启动时暴露。

## 3. 环境与版本注意事项

模型和仿真可以使用同一个 Conda 环境，但只有在 OpenPI、PyTorch、JAX、SAPIEN/MPLib 与
RoboTwin 依赖确实兼容时才建议这样做。共享环境的优点是部署简单，缺点是一次依赖升级可能同时
破坏模型和仿真。

一次已验证的服务器组合包括：

| 组件 | 版本 |
| --- | --- |
| Python | 3.12 |
| PyTorch | 2.7.1 + CUDA 12.8 |
| NumPy | 1.26.4 |
| Transformers | 4.53.2 |
| Tokenizers | 0.21.4 |
| JAX | 0.7.2 |
| Orbax Checkpoint | 0.11.25 |
| SAPIEN | 3.0.0b1 |
| MPLib | 0.2.1 |
| AugMax | 0.3.4 |
| Beartype | 0.19.0 |

这张表记录的是兼容组合，不代表所有机器必须使用相同版本。尤其注意：

- 不要只验证 `import openpi`；这不会加载完整模型依赖。至少验证
  `from openpi.models import model`。
- `augmax`、`beartype` 等缺失通常要到模型深层导入时才出现。
- 不要在已有环境中盲目执行 OpenPI 的整套依赖降级。JAX 与 Orbax 必须配套；例如
  JAX 0.7.2 配合过旧的 Orbax 会在 `jax.experimental.layout.DeviceLocalLayout` 处失败。
- 更新 NumPy、Transformers、JAX、Orbax 或 Pydantic/Numpydantic 后，应重新执行 smoke test，
  不能沿用旧环境的“已经验证”结论。
- 下载依赖和模型应直接在服务器上执行。代理只用于联网，不应通过本机转发大文件。

启动前进行深层导入检查：

```bash
export PYTHONPATH="$SHARED_ROOT/sources/openpi/src${PYTHONPATH:+:$PYTHONPATH}"
"$MODEL_PY" - <<'PY'
import torch
import transformers
import jax
import orbax.checkpoint
from openpi.models import model

print("torch", torch.__version__)
print("transformers", transformers.__version__)
print("jax", jax.__version__)
print("openpi model import: OK")
PY
```

## 4. GPU 渲染和 NVIDIA 图形库

`nvidia-smi` 正常、PyTorch 能看到 CUDA，并不代表 SAPIEN 可以使用 GPU renderer。容器还需要
与驱动兼容的 NVIDIA EGL/GLX/Vulkan 用户态库，并暴露 `graphics` 能力。

检查项：

```bash
nvidia-smi
ldconfig -p | grep -E 'libEGL_nvidia|libGLX_nvidia|libvulkan'
"$SIM_PY" - <<'PY'
import sapien
print(sapien.render.get_device_summary())
PY
```

设备摘要中应能看到所有目标 GPU，且 `Supported` 为真。若这里只看到 CPU renderer，继续增加
worker 不会解决问题。安装 NVIDIA 图形库时应匹配宿主机驱动版本，不要随意安装最新版。

## 5. 启动前的三级验证

不要从“环境刚装完”直接跳到 10,000 episode。

1. **单 worker + fake backend**：验证资产、renderer、观测和环境步进。
2. **单 GPU + 真模型 + 单 episode**：验证 checkpoint、tokenizer、三路图像、joint state、
   14 维动作和模型深层依赖。
3. **单 GPU + 多 worker**：验证动态 batch、session 隔离、显存增长和 worker 回收。

只有三级验证均无基础设施错误后，才启动 8 卡全量。smoke 中 fake policy 的成功率为 0 是正常的；
关键是 episode 能推进、图像有效且没有 error。

## 6. 全量启动配置

仓库脚本 [`run_official_full.sh`](scripts/run_official_full.sh) 是当前全量配置的入口。示例：

```bash
export REPO_ROOT=/path/to/RoboTenon
export SHARED_ROOT=/data/robotwin
export MODEL_PY=/path/to/conda/env/bin/python
export SIM_PY="$MODEL_PY"
export OUTPUT="$SHARED_ROOT/runs/pi05-robotwin2-$(date +%Y%m%d-%H%M%S)"

bash "$REPO_ROOT/extensions/benchmarks/robotwin/scripts/run_official_full.sh"
```

当前脚本的基线性能配置为：

| 参数 | 值 |
| --- | ---: |
| GPU | 8 |
| Worker / GPU | 12 |
| Worker 总数 | 96 |
| 最大 batch size | 32 |
| `wait-ms` | 50 |
| `startup-parallelism` | 12 |
| `enumeration-parallelism` | 24 |
| `recycle-after-tasks` | 8 |
| 调度 | `global_queue` |
| Trace | `summary` |

模型路径和 tokenizer 路径位于
[`configs/robotwin2.json`](../models/pi05-robotwin2/configs/robotwin2.json)，JSON 不展开环境变量。
在其他机器上运行前必须检查并替换为有效绝对路径。

完整运行建议使用 `tmux`、作业系统或 `setsid`，并将 stdout/stderr 写入独立日志。每次运行使用
新的 output 目录；不要复用失败运行的目录，也不要把重启后的结果当作无缝续跑。

## 7. 线程、worker 和内存

大量 worker 下必须限制常见数值库的线程池：

```bash
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false
```

这些变量减少线程争用，但不保证每个进程只有一个线程；SAPIEN、CUDA、文件 I/O 和 Python
runtime 仍可能创建辅助线程。

不要仅按“CPU 核数 ÷ GPU 数”决定 worker 数。RoboTwin worker 在场景加载后可能占用数 GiB
内存，并产生 renderer/驱动显存。此前过高并发曾造成节点失联，因此当前以每卡 12 个 worker
作为保守基线。调优时每次只增加少量 worker，并同时观察：

- 稳态 episodes/min，而不是启动瞬间的 CPU；
- cgroup `memory.current`、`memory.max` 和 `memory.events`；
- worker RSS、GPU 显存和 renderer reset 后是否回落；
- `D` 状态进程数量和共享盘读取延迟；
- worker 回收和重新初始化时间。

`startup-parallelism=12` 只限制同时冷启动的数量。运行初期看不到 96 个 worker、CPU 和内存利用率
偏低是正常的；worker 会分批启动。提高启动并发可以缩短冷启动，但也会同时放大共享盘、内存和
图形驱动压力。

`recycle-after-tasks=8` 用于缓解长期仿真资源泄漏。数值过小会频繁重建场景并降低吞吐，数值过大
则可能让内存或 renderer 状态长期累积。

## 8. 正确理解启动阶段

全量运行大致经历以下阶段：

1. 生成 100 个 profile；
2. 启动 8 个模型副本并加载 checkpoint；
3. 以 24 路并行枚举 10,000 个 episode；
4. reduce 枚举结果并写入全局任务队列；
5. 分批冷启动最多 96 个仿真 worker；
6. 进入稳定仿真与动态合批；
7. 最终 reduce、写入 summary 并关闭服务。

前五个阶段可能持续数分钟。此时常见现象包括：

- GPU 已占用模型显存但利用率接近 0%；
- CPU 大部分空闲；
- episode 数据库先为 0，随后一次出现 10,000 个 pending；
- worker 数缓慢增长；
- 多个进程处于 `D` 状态，等待共享盘、场景资产或驱动初始化。

这些现象不等于卡死。判断是否仍在推进应综合查看子进程 PID 是否周转、数据库更新时间、
`status.json`、GPU 显存、日志和输出文件时间戳。不能只凭一次 `nvidia-smi` 的 0% 利用率下结论。

## 9. 进度、速度和成功率

运行中优先读取：

```text
<output>/runs/evaluation/status.json
<output>/runs/evaluation/episodes.sqlite3
<output>/batch-metrics/*.json
```

`completed` 表示程序正常执行完一个 episode，真实任务是否成功必须看 `task_success`。达到 horizon
且 `task_success=false` 是策略失败，不是基础设施错误。成功率样本很小时应同时报告分子和分母，
例如 `0/2`，不要只写 `0%`。

推荐用框架状态文件读取当前进度：

```bash
export EVAL_OUTPUT=/data/robotwin/runs/example
export EVAL_RUN_ID=evaluation
"$MODEL_PY" - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["EVAL_OUTPUT"]) / "runs" / os.environ["EVAL_RUN_ID"] / "status.json"
status = json.loads(path.read_text())
summary = status.get("summary", {})
print({
    "status": status.get("status"),
    "total": status.get("total_episodes"),
    "completed": status.get("completed_episodes"),
    "active": len(status.get("active", [])),
    "successes": summary.get("successes"),
    "success_rate": summary.get("episode_success_rate"),
    "errors": summary.get("num_errors"),
    "cancelled": summary.get("num_cancelled"),
})
PY
```

速度应至少区分三种口径：

- **端到端速度**：完成数 ÷ 从主进程启动到当前的时间，包含模型加载和冷启动；
- **稳态速度**：最近 5–10 分钟完成数 ÷ 时间，更适合估算剩余时长；
- **单 episode 仿真速度**：环境步数 ÷ episode 执行时间，用于发现仿真退化。

首批 episode 往往需要跑满 horizon 才完成，因此“96 个正在运行、完成数仍很少”在开始阶段是正常的。
等至少数十个 episode 完成后再估算 ETA 和成功率。

`batch-size=32` 是上限，不是实际 batch。实际分布必须从 `batch-metrics/*.json` 读取。请求异步到达时，
即使有 96 个 worker，平均 batch 也可能远小于 32。

## 10. 常见故障

### 模型服务启动后立即退出

先在同一解释器中执行 OpenPI 深层导入，检查缺包和 JAX/Orbax 兼容性。顶层 import 成功不足以证明
checkpoint 可以加载。

### 8 张卡都有显存但利用率为 0%

先确认是否仍处于 episode 枚举、reduce 或 worker 冷启动阶段。若数据库已有 running episode，继续
检查 worker 是否产生观测请求、batch metrics 是否增长，以及 SAPIEN 是否识别 GPU renderer。

### CPU 和内存利用率很低

查看 worker 数和进程状态。低利用率通常表示 worker 尚未全部启动，或大量进程在 `D` 状态等待资产
和驱动初始化；不是简单增加 `OMP_NUM_THREADS` 就能解决。

### CPU 很高但吞吐不升

检查是否忘记限制 BLAS/OpenMP 线程、worker 是否过多、共享盘 reset 是否变慢，以及实际 batch 是否
仍集中在 1。减少 worker 有时比继续增加并发更快。

### 内存持续上升或节点失联

降低 worker 数，保留 `recycle-after-tasks`，并检查 cgroup 而不是只看宿主机 `free`。节点无 swap 时，
cgroup OOM 或平台健康检查失败都可能表现为服务器突然断开。恢复后不要假设本地环境、软链接和未提交
代码仍存在。

### 运行结果成功率很低

先区分三类结果：任务未成功、跑满 horizon、基础设施 error。只有第三类说明 benchmark 接入或环境
可能有问题。不要通过增大 horizon、过滤失败 episode 或重试普通策略失败来“修复”分数。

## 11. 节点重建后的恢复顺序

服务器或容器重建后按以下顺序恢复，避免一边启动一边补依赖：

1. 同步完整 RoboTenon 工作树，包括未提交但确实需要的 benchmark 改动；
2. 恢复 `/app/RoboTwin` 指向完整仿真源码与资产；
3. 检查 checkpoint、tokenizer、OpenPI 源码和输出盘空间；
4. 恢复 Conda 环境与 editable installs；
5. 验证 NVIDIA EGL/GLX/Vulkan 和 SAPIEN GPU 设备；
6. 执行 OpenPI 深层 import；
7. 运行单 episode 真模型 smoke；
8. 启动全量并确认主进程持续存活；
9. 确认 8 个模型副本加载、10,000 个 episode 入队、96 个 worker 分批启动；
10. 等首批 episode 完成后再报告速度、成功率和 ETA。

只有完成第 8–9 步并通过存活检查，才能称为“全量已经拉起”。仅看到启动命令返回或生成 profiles，
不能视为启动成功。

## 12. 最终报告检查清单

评测结束后至少记录：

- 代码、RoboTwin、OpenPI 和 checkpoint 版本；
- 任务覆盖范围、episode 总数、seed 和 horizon 来源；
- GPU、CPU、内存和模型/仿真环境版本；
- worker、batch、wait、回收、调度和超时参数；
- 总耗时、端到端与稳态速度；
- 完成、成功、未成功、error 和 cancelled 数；
- Easy、Hard、逐任务及总体成功率；
- 实际 batch size 分布；
- 是否开启视频、逐步 trace 或其他影响性能的 recorder；
- 已知偏差和失败原因分类。

最终只在 `10,000/10,000` 且 error/cancelled 已明确说明后报告全量结果。运行中得到的部分成功率，
尤其首批少量 episode 的成功率，不能作为最终模型成绩。
