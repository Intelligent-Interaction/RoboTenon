# 通用评测加速候选：本地验证

[English](benchmark-optimization-candidates-2026-09-29.md) · [**简体中文**](benchmark-optimization-candidates-2026-09-29.zh-CN.md)

## 结论

优先验证公共 runtime 的精简 trace 模式；暂不为当前 Xiaomi 全量评测实现历史增量缓存或跨 GPU session 迁移。初始化并发与 CPU 线程配置需要服务器上的真实负载对照，不能用本地合成任务决定生产参数。本轮只新增独立测试工具和证据，没有修改生产路径或干预服务器任务。

工作区存在正在进行的统一 system 接口改动，本次保留这些改动。测量环境为 macOS ARM64、10 个逻辑 CPU、Python 3.11.10；没有使用真实 GPU、模型权重或仿真资产。Graph 测试执行了真实框架；队列测试属于离散事件模拟。

## 1. 逐节点 trace：值得优先做可选精简模式

真实 `run_episode` 执行 400 个顺序节点，每个节点返回一个小结果。使用真实 `SharedObservability` Unix Socket 汇聚与落盘路径；比较保持实时状态、关闭 trace、同时关闭实时状态三种组合。每种先预热一次，再交替执行五次，验证 400 个节点全部执行且结果一致。时间包含配置读取、Graph、episode 输出和观测服务关闭，不包含服务启动。

| 模式 | 中位耗时 |
|---|---:|
| trace + 实时状态 | 885.61 ms |
| 关闭 trace，保留实时状态 | 691.21 ms |
| 关闭 trace 和实时状态 | 689.99 ms |

关闭 trace 减少约 **22% 空载 Graph 耗时**，约 0.49 ms/节点（摊销值，并非真实仿真分段计时）。关闭已有节流的实时状态只再省约 1.22 ms，低于本次样本波动，没有证据支持继续砍实时状态。

建议实现可选的公共摘要/采样 trace 模式，保留最终结果、错误、计数和关键生命周期。完全关闭 trace 在这里只是收益上界，不能直接作为生产方案。真实仿真的物理、渲染和推理时间不在本测试中，不能宣称整场评测快 22%。本地测试也没有复现 128 个进程同时写入服务器共享存储的竞争。

## 2. 历史增量缓存：按后端声明决定，当前 Xiaomi 不值得

使用现有 MessagePack 编解码，输入为三路 256×256 RGB、状态、4 帧历史，采样间隔 2。原型按帧编号发送缺失帧，在接收端保留有界缓存，重建完整历史；逐元素验证图像和状态完全一致。对比包括缓存维护及编解码，不包含 Socket、GPU 或模型推理。五轮交替测量，排除第一条请求。

| replan_steps | 完整历史 | 增量原型 | 编解码比值 | 平均字节变化 |
|---|---:|---:|---:|---:|
| 1 | 1.000 ms | 0.215 ms | 4.66× | 2.36 MB → 0.65 MB |
| 2 | 0.972 ms | 0.200 ms | 4.86× | 2.36 MB → 0.59 MB |
| 4 | 1.061 ms | 0.533 ms | 1.99× | 2.36 MB → 1.18 MB |
| 16 | 1.240 ms | 1.217 ms | 1.02×（噪声量级） | 2.36 MB → 2.36 MB |

Xiaomi 声明 `length=4, interval=2, replan_steps=16`，相邻模型请求没有重叠帧。初轮测试在此场景甚至略慢，重跑略快；稳定结论是**没有传输字节节省**。把每个环境 step 的新帧都发到模型服务，反而可能从每 16 步发送 4 帧增加到发送 16 帧。

短 replan 后端可考虑通用按需缺失帧协议，但还需要断连、重试、session 清理与跨服务路由测试，当前原型不是生产实现。

## 3. 跨 GPU 全局推理队列：收益未证实

闭环离散事件模拟：128 个客户端、8 个服务、batch 上限 8、等待 50 ms，每个客户端 100 次请求；两种调度复用每个客户端完全相同的模拟延迟序列。三个随机种子取加速比中位数，并检查 12,800 个请求无丢失或重复。

假设单样本推理 0.4 秒，每个额外样本增加 0.02、0.1 或 0.4 秒；仿真等待为指定均值的 0.5–1.5 倍。这里不是拟合过的 GPU 性能曲线。

| 平均模拟等待 | 全局队列相对固定绑定的加速比范围 |
|---|---:|
| 2 秒 | 0.994–1.056× |
| 6 秒 | 0.997–1.013× |
| 12 秒 | 0.994–1.003× |

例如等待 6 秒、额外样本 0.02 秒时，平均 batch 从约 1.37 增到 1.96，但速度只约 1.3% 改善。**更大的 batch 不等于更高的端到端吞吐。**

模拟乐观地忽略 session 迁移、全局队列通信、不同 batch key、不均匀 GPU 速度、NUMA 和 RNG 约束。它也未覆盖严重的 GPU 负载不均，不能证明全局队列在所有场景收益小。当前结果不足以支持立即重构，应先获得真实 batch 1/2/4/8 服务曲线和请求到达时间，再回放比较。`official_stream` 的跨服务调度会改变 RNG 分配。

## 4. 初始化并发与 CPU 并发：只能条件化判断

实际运行公共 `startup_gate`，使用预热后的 32 个本地进程，64 项任务，闸门 8/16/32；分别执行 50 ms 等待和 SHA-256 CPU 工作，每组测量三次。进程启动不计时。

| 合成负载 | 并发 8 | 并发 16 | 并发 32 |
|---|---:|---:|---:|
| 等待型 | 0.454 s | 0.231 s | 0.112 s |
| CPU 型 | 0.244 s | 0.220 s | 0.214 s |

等待型接近线性改善，CPU 型 8→32 只节省约 12% 时间，16→32 已不足 3%。这些控制实验不模拟真实资产加载、EGL、GPU 竞争和数值库线程，因此不能据此推荐服务器改成 32。

纠正此前判断：`native_reset_parallelism` 的实际闸门是 Worker bootstrap，第一次 reset 和观测成功后释放。它不是对所有 episode 的 reset 都限流。提高它主要改善冷启动，无法解决当前已进入稳态的吞吐。

Worker 数量、BLAS/OpenMP 线程和 CPU 亲和性尚未在真实仿真负载下比较；本次 CPU 哈希任务不验证这些参数。需要下一轮用相同任务/seed、真实 step/render 计时做服务器 A/B。

## 复现与证据

在包含 NumPy、msgpack 和框架测试依赖的 Python 环境中，从仓库根目录运行：

```bash
PYTHONPATH=.:tests:extensions/benchmarks/benchmark-support/src \
python extensions/benchmarks/benchmark-support/tools/benchmark_optimization_candidates.py \
  --output /tmp/local-candidates.json

PYTHONPATH=.:extensions/benchmarks/benchmark-support/src \
python extensions/benchmarks/benchmark-support/tools/benchmark_worker_capacity.py \
  --output /tmp/local-worker-capacity.json
```

- [历史、Graph 和队列证据](evidence/common-performance/local-candidates.json)
- [并发控制实验](evidence/common-performance/local-worker-capacity.json)

这些结果用于候选筛选，不代表所有 benchmark 的真实加速比例，也不构成评测完成时间的保证。
