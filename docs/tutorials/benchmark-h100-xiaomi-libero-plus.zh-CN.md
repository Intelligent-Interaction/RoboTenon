# Xiaomi Robotics 0 + LIBERO-Plus：复现指南与实测参考

[English](benchmark-h100-xiaomi-libero-plus.md) · [**简体中文**](benchmark-h100-xiaomi-libero-plus.zh-CN.md)

这篇文档面向希望复现或扩展 LIBERO-Plus 评测的读者。它不只给最终数字，也说明如何启动、
结果应该怎样解释、哪些配置不能照抄，以及遇到低成功率、低 GPU 利用率或 horizon 失败时
应该先检查什么。文中不包含服务器地址、账号、内部目录、运行 ID 或文件哈希。

本文使用 RoboTenon 统一入口，在 LIBERO-Plus 四个 suite 上评测
[`XiaomiRobotics/Xiaomi-Robotics-0-LIBERO`](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-0-LIBERO)
的完整配置、结果和速度。模型原本面向基础 LIBERO 发布，因此这里衡量的是模型面对
LIBERO-Plus 视觉、语言、布局和传感器扰动时的迁移鲁棒性，不代表 Xiaomi 官方发布的
LIBERO-Plus 成绩，也不能直接与基础 LIBERO 分数横向比较。

安装与资产准备见 [LIBERO-Plus SETUP](../../extensions/benchmarks/libero_plus/SETUP.zh-CN.md)，
模型适配见 [Xiaomi Robotics 0](../../extensions/benchmarks/models/xiaomi-robotics0/README.zh-CN.md)。

## 先看结论

本次完整运行于 **2026 年 10 月 10 日**结束。**8 张 H100 在 2 小时 7 分 57 秒内完成 10,030 个案例，成功 7,499 个，最终成功率为
74.77%。** 端到端平均吞吐为 **78.39 episodes/分钟**；全程无执行错误和取消。

| 指标 | 结果 | 统计口径 |
| --- | ---: | --- |
| 完成量 | 10,030 / 10,030 | 四个 suite 的全部枚举案例，每个案例 1 个 episode |
| 成功 / 任务失败 | 7,499 / 2,531 | 以环境最终任务成功信号统计 |
| 最终成功率 | 74.77% | 7,499 ÷ 10,030；每个案例权重相同 |
| 错误 / 取消 | 0 / 0 | evaluator 最终汇总中的错误与取消计数 |
| 完整耗时 | 7,677.13 秒，约 127.95 分钟 | 包含模型服务启动、Worker 执行和服务关闭，不含下载与安装 |
| 端到端吞吐 | 78.39 episodes/分钟 | 完成量 ÷ 完整耗时 |
| 模型请求 / batch 调用 | 201,028 / 137,997 | 8 个模型服务最终计数之和 |
| 实际平均 batch | 1.46 | 请求数 ÷ batch 调用数；配置上限 32 不等于实际平均值 |

### 各 suite 结果

| Suite | Horizon | 成功 / 总数 | 成功率 |
| --- | ---: | ---: | ---: |
| `libero_spatial` | 220 | 1,971 / 2,402 | 82.06% |
| `libero_object` | 280 | 1,925 / 2,518 | 76.45% |
| `libero_goal` | 300 | 1,775 / 2,591 | 68.51% |
| `libero_10` | 520 | 1,828 / 2,519 | 72.57% |
| **合计** | — | **7,499 / 10,030** | **74.77%** |

四个 suite 的案例数不同，因此总分按 episode 数加权，而不是四个 suite 成功率的简单平均。

### 各扰动类别结果

| 扰动类别 | 成功 / 总数 | 成功率 |
| --- | ---: | ---: |
| Background Textures | 994 / 1,076 | 92.38% |
| Camera Viewpoints | 658 / 1,599 | 41.15% |
| Language Instructions | 1,376 / 1,537 | 89.53% |
| Light Conditions | 1,059 / 1,142 | 92.73% |
| Objects Layout | 1,151 / 1,525 | 75.48% |
| Robot Initial States | 876 / 1,550 | 56.52% |
| Sensor Noise | 1,385 / 1,601 | 86.51% |

相机视角变化是本次评测中最明显的薄弱项，其次是机器人初始状态变化。背景纹理、光照、
语言表达和传感器噪声的成功率相对较高。这些差异反映当前 checkpoint 在本次协议下的表现，
不能单独归因于某个模型结构或训练数据因素。

### 各难度等级结果

| 难度 | 成功 / 总数 | 成功率 |
| --- | ---: | ---: |
| 1 | 1,494 / 1,644 | 90.88% |
| 2 | 1,872 / 2,202 | 85.01% |
| 3 | 1,668 / 2,094 | 79.66% |
| 4 | 1,366 / 1,886 | 72.43% |
| 5 | 994 / 2,083 | 47.72% |
| 未标注 | 105 / 121 | 86.78% |

成功率随标注难度总体下降，难度 5 的成功率为 47.72%。难度级别的案例数量并不相同，
总分仍以全部 10,030 个 episode 为分母。

### 失败终止原因

| 终止原因 | 数量 | 说明 |
| --- | ---: | --- |
| `environment_terminal` | 7,499 | 环境报告任务成功并正常终止 |
| `horizon` | 2,531 | 未在对应 suite 的原生最大步数内成功 |
| 执行错误 | 0 | 没有模型服务、仿真 Worker 或协议执行错误 |
| 取消 | 0 | 没有被取消的 episode |

`horizon` 是普通任务失败，不是超时异常。评测保留各 suite 原生 horizon，没有为了提高分数
统一延长最大步数；成功后会立即结束 episode。

## 硬件与运行配置

| 项目 | 本次配置 |
| --- | --- |
| GPU | 8 × NVIDIA H100 80GB HBM3 |
| CPU | 2 × Intel Xeon Platinum 8558，共 96 物理核、192 逻辑 CPU |
| 内存 | 约 2 TiB |
| 模型副本 | 每卡 1 个，共 8 个 |
| 仿真 Worker | 每卡 36 个，共 288 个 |
| batch | 每个模型副本上限 32，等待窗口 100ms |
| 调度 | 四个 profile 合并、全局队列、先 shuffle、调度 seed 7 |
| Worker 回收 | 每完成 8 个任务重建一次 |
| 视频 / 完整 trace | 均关闭；trace 使用 summary 模式 |
| 模型输入 | agentview + wrist 两路 256×256 RGB、8 维 proprioception |
| 模型输出 | 连续 7DoF action，每次最多执行 10 步后重新规划 |

运行结束后复查的仿真环境包含 Python 3.12、NumPy 1.26、robosuite 1.4 和 MuJoCo 3.8；
模型环境包含 Python 3.12、PyTorch 2.7、Transformers 4.57，并以 bfloat16 和 FlashAttention 2
加载模型。这里不是完整依赖锁，安装和复现仍应以仓库中固定的依赖与
[SETUP](../../extensions/benchmarks/libero_plus/SETUP.zh-CN.md)为准。

为减少 288 个仿真进程之间的 CPU 线程争用，本次运行限制常用数值库为单线程：

```bash
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false
export MALLOC_ARENA_MAX=2
```

这些变量限制的是相应库的内部线程池，不是整个进程的硬 CPU 配额。

## 协议与模型适配

完整评测将 `all-0`、`all-1`、`all-2`、`all-3` 四个 profile 合并到同一份 episode plan，
分别覆盖 `libero_spatial`、`libero_object`、`libero_goal` 和 `libero_10`。每个枚举案例执行
1 个 episode，环境 seed 为 7，reset 后等待 10 个环境步再开始控制。

Xiaomi Robotics 0 后端复现官方 LIBERO 推理接口的关键输入输出约定：

- 使用 base 和 left-wrist 两路相机及官方 prompt 模板；
- 将 8 维 simulator state 补齐为 checkpoint 使用的 32 维 state；
- 按 observation 生成稳定随机种子，动态合批时仍为每个请求保留独立噪声；
- 输出连续 7DoF action chunk，每次消费 10 步后重新规划；
- 模型 history 为 1 帧，因此本次运行未启用历史增量缓存。

这保证统一 runner 没有随意改变模型接口，但不等于复现 Xiaomi 官方基础 LIBERO evaluator
的 benchmark 协议：LIBERO-Plus 增加了大量扰动案例，任务分布和计分集合不同。

## 并行效率与 batch 分布

动态合批将 201,028 个推理请求合并成 137,997 次模型调用，减少 63,031 次调用，调用次数
下降 31.35%。这只是模型调用次数的减少比例，不是端到端加速倍数；仿真、reset、进程回收
和 CPU 调度仍占用大量时间。

| 实际 batch size | 调用次数 | 占全部调用 |
| ---: | ---: | ---: |
| 1 | 88,542 | 64.16% |
| 2 | 38,197 | 27.68% |
| 3 | 9,285 | 6.73% |
| 4 | 1,669 | 1.21% |
| 5 | 268 | 0.19% |
| 6 | 31 | 0.02% |
| 7 | 5 | <0.01% |
| 8–32 | 0 | 0% |

虽然 batch 上限设置为 32，但本次实际峰值只有 7，平均为 1.46。原因是仿真 Worker 的请求
到达时间分散，100ms 等待窗口内很少能聚合出更大的 batch。继续提高配置上限不会自动提高
吞吐；应联合比较 Worker 数、等待窗口、GPU 利用率、CPU 压力和最终轨迹一致性。

## 全量启动命令

以下命令使用公开、可移植的占位路径。先按 SETUP 准备模型和仿真环境，再在仓库根目录执行：

```bash
export MODEL_PY=/opt/envs/xiaomi-libero/bin/python
export SIM_PY=/opt/envs/libero-plus/bin/python
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false MALLOC_ARENA_MAX=2
export EVAL_OUTPUT="runs/xiaomi-robotics0-libero-plus-$(date +%Y%m%d-%H%M%S)"

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero_plus --profiles all-0 all-1 all-2 all-3 \
  --backend ./extensions/benchmarks/models/xiaomi-robotics0/vla.py \
  --backend-config ./local-configs/xiaomi-robotics0-libero.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 36 --batch-size 32 --wait-ms 100 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --episodes-per-task 1 --startup-parallelism 8 \
  --recycle-after-tasks 8 --max-retries 1 \
  --task-timeout 14400 --inference-timeout 600 \
  --model-startup-timeout 1800 --worker-startup-timeout 1800 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output "$EVAL_OUTPUT"
```

对应的本地模型配置只需要指向已下载 checkpoint，不要提交机器私有路径：

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-0-LIBERO",
  "device": "cuda",
  "metrics_dir": "/data/runs/xiaomi-robotics0/metrics"
}
```

首次部署时不要直接使用 288 个 Worker。先用单卡、单 Worker、batch 1 和
`--max-tasks 1 --episodes-per-task 1` 做 smoke test，再逐步增加 Worker 并观察 CPU、内存、
GPU 显存和稳定吞吐。

## 常见错误和踩坑点

### 1. 把基础 LIBERO 分数当作 LIBERO-Plus 预期值

两者可以使用同一个 checkpoint，但协议完全不同。基础 LIBERO 测固定任务与 initial states；
LIBERO-Plus 枚举相机、纹理、光照、语言、布局、机器人初始状态和传感器噪声扰动。本文的
74.77% 与[基础 LIBERO 的 97.15%](benchmark-h100-xiaomi-libero.zh-CN.md)回答的是不同问题。

### 2. 只运行 `all-0`

`all-0` 只有 `libero_spatial`，不是完整 LIBERO-Plus。使用
`--profiles all-0 all-1 all-2 all-3` 才能覆盖四个 suite。合并 profile 后由同一全局队列
shuffle 和 reduce，避免按 suite 依次运行时反复重启模型并造成阶段性 GPU 空闲。

### 3. 不同 LIBERO fork 导入了同名包

基础 LIBERO、LIBERO-Plus 和 LIBERO-Pro 都可能导入名为 `libero` 的 Python 包。不要把它们
安装进同一个仿真环境，也不要共用一份会被覆盖的 LIBERO 配置。出现任务数量不对、资产路径
跳到另一套目录或类签名不匹配时，先用 `SIM_PY` 打印实际模块路径。

### 4. 下载了 demonstration，却缺在线仿真资源

评测需要 BDDL、initial states、mesh、texture 和 LIBERO-Plus 的变体配置，不需要训练用的大型
demonstration 数据。按 SETUP 下载在线评测资产；数据量很大不等于资产已经齐全。

### 5. 把 horizon 当作框架错误

本轮 2,531 个未成功案例全部正常运行到了各 suite 原生 horizon，基础设施错误为 0。统一把
horizon 改成 1,000 会改变协议并通常显著增加尾部耗时；应先按 category 和 task 找出失败集中点。

### 6. batch 上限 32，却期待 GPU 始终满载

本轮 64.16% 的模型调用仍是 batch 1，实际最大 batch 只有 7。仿真请求到达不同步时，继续
增大上限没有帮助。GPU 利用率低时应一起检查 CPU 争用、仿真步速、reset 和 `wait-ms`，不能
只看命令里的 `batch-size`。

### 7. Worker 越多不一定越快

36 Worker/卡依赖 96 个物理 CPU 核和约 2 TiB 内存。Worker 太多会增加内存峰值、上下文切换
和 reset 竞争，甚至触发 OOM。新机器从低并发逐步压测，以稳定 episodes/min 而不是进程数
作为调优依据。

### 8. 模型能用 CUDA，不代表仿真已经 GPU 渲染

模型和 MuJoCo 渲染是两条独立链路。Headless 环境通常需要 `MUJOCO_GL=egl`，还要保证仿真
进程能看到正确的 EGL 设备。只有第一张卡有利用率时，检查 Worker 到设备的绑定和渲染后端，
而不是只检查 PyTorch 的 CUDA 可用性。

### 9. 为排错开启录像后仍引用本文速度

本轮关闭视频和完整 trace。录像对少量失败任务很有用，但全量开启会引入编码和磁盘 I/O，
速度不能再与本文的 127.95 分钟直接比较。建议先按失败类别抽样录制。

### 10. 隐式丢弃 error 后计算成功率

报告结果时同时给出计划数、完成数、success、error 和 cancelled。本文没有 error 或 cancelled；
若其他运行存在错误，不能把错误 episode 从分母静默删除后仍称为完整协议结果。

## 结果适用范围

- 这是单次完整运行，每个枚举案例只有一个 episode，没有多 seed 重复实验或置信区间。
- 全局 shuffle 与动态 batch 会改变请求时序；模型虽保留逐 observation 随机种子，浮点执行和
  仿真调度仍不应被解释为跨硬件逐位一致。
- 本次关闭视频录制，因此耗时不能直接用于估算开启视频后的吞吐。
- 最终 74.77% 是 LIBERO-Plus 全部案例的 episode 加权成功率，不是四个 suite 的简单平均，
  也不是 Xiaomi 官方基础 LIBERO 报告分数。
- 硬件、依赖、checkpoint、horizon、相机规格或协议子集发生变化时，应作为新的实验单独报告。
