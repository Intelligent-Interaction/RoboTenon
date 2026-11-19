# 基准共享性能优化（2026-09-29）

[English](common-benchmark-performance-2026-09-29.md) · [**简体中文**](common-benchmark-performance-2026-09-29.zh-CN.md)

## 范围

改动位于核心的共享批处理、传输逻辑和 benchmark-support。各基准的仿真适配器及模型后端沿用原实现。依用户要求撤回了 Xiaomi 预处理的临时改动。Worker 数、GPU 路由、episode 顺序、组批等待策略、模型 RNG、观测及 horizon 保持原设置。本轮初始工作没有部署至正在运行的服务器评测。

## 修改

- 本地批处理 RPC 的新消息使用 MessagePack 加 NumPy 原始字节，取代 JSON 加 Base64。保留数值数组的 dtype、形状和大小限制，继续拒绝 object 数组。新接收端可读旧 JSON，旧接收端不能解码新消息，模型和仿真进程须一并升级。框架安装现在依赖 msgpack。
- 同步与实时路径的共享历史直接借用已封装快照的只读视图，省去第二次复制。模拟器数据首次写入快照仍会复制；公开解码接口默认仍返回可写副本。数组的生命周期由其 NumPy base 管理，后续仿真步无法覆盖快照拥有的数据。
- 共享策略复用已经校验和规范化的动作数据，省去第二次 JSON 兼容值转换。
- 每个共享模型副本在启动、每隔五秒及关闭时原子发布小型聚合快照，包含批大小直方图、请求数、推理和排队耗时、当前及峰值队列深度、会话数。无需维护不断增长的逐请求历史。

## 本地 CPU 测量

基线提交为 `014dad454912447ec7bd41055280f05006735ea6`。
每份负载含 4 帧历史观测，各有 3 路 256×256 uint8 RGB 图像及 float32 状态。数据为与模型无关的合成样本。运行环境为本地 macOS ARM64、Python 3.11.10、NumPy 2.4.6，预热后测量 30 次。

| 操作 | 修改前中位数 | 修改后中位数 | 比值 |
| --- | ---: | ---: | ---: |
| 编解码往返 | 12.420 ms | 1.134 ms | 10.96× |
| 为历史记录解码快照 | 0.00973 ms | 0.00296 ms | 3.29× |
| 真实 Unix socket 完整负载回显 | 34.819 ms | 9.818 ms | 3.55× |

消息大小从 3,147,240 降至 2,360,359 字节，减少约 25%。已核对重建的图像与状态完全一致。回显测试双向传输完整观测，实际推理返回较小的动作，因此回显结果衡量传输压力，不能预测 episode 时延。快照解码在该样本中仅节约微秒级时间。

安装框架及 benchmark-support 后，在仓库根目录复现。

```bash
python extensions/benchmarks/benchmark-support/tools/benchmark_transport.py --output /tmp/common-transport.json --repeats 30
```

[原始测量](evidence/common-performance/local-transport.json)。

## 正确性验证

共享测试以新传输格式覆盖全部 20 个基准的模拟动作配置，同时覆盖只读历史快照所有权、非连续数组、指定字节序的数组、布尔/整数/浮点数组、旧消息、无效负载和限制。进程测试验证实时指标、单请求与批处理、会话隔离和关闭。还运行了原生 Graph、统一后端、实时执行、执行层及入口审计的既有回归。

`test_benchmark_performance_paths`、`test_dynamic_batching`、`test_native_graph_configs`、`test_unified_benchmark_backend`、`test_live_benchmark`、`test_benchmark_entry_audit`、`test_live_status_window` 和 `test_execution_backends` 共 **140 项通过，用时 25.98 秒**。

本轮没有重新运行全部 20 个真实模拟器，也没有验证 GPU 动作一致性或全量评测吞吐。未调节 Worker 数量、亲和性或跨 GPU 调度；这些变更需要稳定负载的测量，并单独核对协议与 RNG。

## 服务器验证 c18ab58c

前次评测在完成 153 个 episode、124 次成功、零错误时停止。已保留结果及 `manual-stop.json`，全部 140 个受管进程收到 SIGTERM 后退出。模型和仿真进程随后使用新源码树。服务器仿真环境直接安装了 MessagePack，模型环境已经包含该依赖。

- 同一组 140 项回归在服务器上全部通过。
- 真实 Xiaomi 冒烟使用 8 张 GPU，每卡 16 个仿真 Worker，动态组批上限 8，收集超时 50 ms。**128/128 个 episode 正常完成且无错误**，对应 128 个不同 Worker 和 128 个唯一 episode ID。8 个任务各有 16 个 episode，每个执行 32 个环境步。此处刻意缩短了 horizon，用于验证运行链路。
- 8 个模型服务均报告已关闭，待处理请求和会话归零。8 张 GPU 的显存占用均回到 0 MiB。
- 实际批大小为 246 个单样本批次和 5 个双样本批次，共 256 个请求。错峰初始化和短 episode 没有形成大小为 8 的满批，因此未验证饱和吞吐。
- 另一次真实 OpenDrawer 执行验证了动作后三路 256×256 相机均有变化、传输图像逐像素相同、历史借用视图只读，并正常返回终止信号和布尔成功值。
- 服务器 CPU 传输微基准中，编解码耗时从 14.50 降至 0.83 ms，Unix socket 回显从 33.72 降至 7.26 ms（4.65×）。数据衡量通信开销，未计入完整评测耗时。

运行目录为 `/path/to/deployment/xiaomi-128worker-smoke-20260929-143300`。冒烟正常结束，本次验证没有发起新的 target50 × 50 全量评测。

证据见[运行检查](evidence/common-performance/server-validation.json)、[图像哈希](evidence/common-performance/server-render-check.json)和[传输时间](evidence/common-performance/server-transport.json)。
