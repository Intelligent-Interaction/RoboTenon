# 公共 trace 摘要与历史缓存实现

[English](trace-history-implementation-2026-09-29.md) · [**简体中文**](trace-history-implementation-2026-09-29.zh-CN.md)

## 范围

适用于所有通过统一入口运行的 benchmark，没有新增 RoboCasa 或 Xiaomi 专用判断。保留 Side Chat 的 system Graph、planner/verifier Tool、subgoal 和 action chunk 状态接口。本轮没有部署或重启正在运行的服务器评测。

- 公共 `TraceClient` 和 Graph/Tool 调用路径：summary 模式省略成功调用的详细输入、输出和观测引用序列化，记录有界聚合。异常及失败 Tool 保留独立错误记录；episode 最终结果与实时状态不变。full 模式保留原路径。
- 公共 `BatchClient` / 服务连接：历史帧内容哈希、缺失帧传输和连接级 LRU。缓存存放序列化数据，不共享模型收到的可写数组；缓存命中也会重建独立数组。
- `SharedBatchModel` 和同步/实时 benchmark runtime：统一选项传递与失败客户端清理。`vla.py` 无需修改。
- 统一 evaluate：`trace_mode=summary`、`history_cache=auto`；报告记录实际生效状态。模型与仿真环境仍隔离，调度、任务 seed、horizon、RNG 和 success 行为没有修改。

## 内存与恢复边界

每个缓存最多 32 帧或 64 MiB，客户端与服务端各自保留一份；history 太大回退普通消息。连接断开时服务端销毁缓存，客户端错误路径清缓存并从 Tool 移除失败连接。新连接对应新 session，首条请求重新发送所有帧，不透明重试推理。模型服务与 Worker 必须同时升级。

缓存并不是压缩；无历史重叠时强制开启会增加哈希与序列化开销。自动模式只按后端声明筛选可能有收益的情况，真实收益仍需测量。

## 服务器内存只读诊断

服务器仍运行 `c18ab58c`，没有启用本次新增缓存。两次约间隔 4.2 分钟的采样中，128 个仿真 Worker 的 RSS 合计从约 616.3 GiB 增至 624.5 GiB；后一次全机可用内存约 314 GiB，文件缓存约 287 GiB。模型/启动进程 RSS 合计约 17.5 GiB。RSS 合计包含共享页，不能直接等同物理占用；抽查最大 Worker 的约 6 GiB 主要为私有匿名内存。

因此内存增长不只是文件缓存，也不是本次未部署的历史缓存造成。当前 `recycle_after_tasks=1000`，每个 Worker 平均约 20 个任务，基本不会触发任务计数回收。仅凭这些采样不能断定原生仿真器泄漏；下一步应检查 episode 边界资源释放，并考虑公共 Worker 内存水位回收。该内存回收机制不在本次实现中。

## 验证

测试覆盖缓存像素/状态精确一致、重复帧复用、LRU 边界、无效消息、回退、输入原地修改隔离、多个 session、batch 1/2、重连与超时清理；summary 模式计数有界、异常不丢失、Graph 执行与实时终止状态一致。还包含 Side Chat 的多模型系统及同步/实时 Graph 回归。

生产 RPC 微测试使用真实 BatchClient、BatchEngine、Unix Socket 和公共编码器；模型仅返回状态编号，故不包含 GPU 推理。三路 256×256 图像，history=4、interval=2，每种模式 140 个测量样本，交替运行、排除前五次预热。证据见 [RPC 测量](evidence/common-performance/production-history-cache.json)。

真实 Graph summary 对照证据见 [Graph 测量](evidence/common-performance/production-trace-summary.json)。测试工具位于 `extensions/benchmarks/benchmark-support/tools/benchmark_history_cache.py` 和 `benchmark_optimization_candidates.py`。指标只代表本地公共路径，不代表整场评测或所有仿真器的加速比例。

## 最终本地结果

- 197 项回归测试通过（37.66 秒），包含统一入口、实时路径、多模型系统、20 个 benchmark 公共配置与性能路径。
- 隔离重跑的 400 节点 Graph：full 891.57 ms → summary 726.01 ms，耗时减少 18.6%。不是仿真端到端加速。
- replan=2 的真实 RPC 微测试：关闭缓存 4.73 ms，开启缓存 2.45 ms，比值 1.93×。
- replan=16 的真实 RPC 微测试：关闭缓存 5.04 ms，开启缓存 6.63 ms，比值 0.76×。
- replan=16 强制开启缓存反而更慢，默认 auto 对当前 Xiaomi 保持关闭。
- `git diff --check` 通过。未提交、未推送、未部署到正在运行的服务器。
