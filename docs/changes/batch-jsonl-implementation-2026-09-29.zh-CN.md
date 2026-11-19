# 公共 metrics 批量 JSONL 写入（2026-09-29）

[English](batch-jsonl-implementation-2026-09-29.md) · [**简体中文**](batch-jsonl-implementation-2026-09-29.zh-CN.md)

## 修改

`robotenon/infra/metrics.py` 的 MetricsDaemon.ingest_batch 现在每批只获取一次锁、打开一次文件、执行一次批量追加并flush；单条ingest通过同一路径。所有使用公共metrics服务的benchmark自动受益，不需要修改extension配置。

格式、校验、histogram边界、计数聚合和JSONL增量读取保持兼容。批次中途遇到非法记录或生成器异常时，已接收的有效前缀仍在finally中落盘，随后向调用方报告错误。写入后记录文件身份和偏移，避免首次查询重复重放本进程刚写入的数据。

保留flush-only持久性，没有增加fsync或新的定时缓冲，也没有修改trace/episode存储、stdout/stderr、模型batch指标快照。日志不是数据库事务；磁盘故障或进程在写入中崩溃时，不承诺整批原子提交。当前公共服务仍负责单一文件的写入，多个独立进程直接写同一个JSONL不属于本次支持范围。

## 回归

55项通过：新增批量日志测试、现有metrics/trace、Portal、observability runtime、routing与trace summary。

覆盖Unicode、增量查询、重新加载、重复查询不重复计数、非法label、histogram边界冲突、输入迭代器异常的有效前缀落盘、并发唯一性/聚合、只读限制和读取端文件替换检测。

## 正式实现性能

本地 macOS-27.0-arm64-arm-64bit / Python 3.11.10。65536条生产结构的合成metrics，3轮中位数；128个逻辑生产者，最多32个活跃Unix socket连接，每次64条。基线类保留优化前逐条文件写入代码，复用相同校验和内存聚合。原型和正式实现分别报告，不混用。

| 指标 | 逐条JSONL基线 | 正式批量JSONL |
|---|---:|---:|
| IPC总耗时 | 3.422 s | 1.144 s |
| IPC请求P95 | 132.4 ms | 41.0 ms |

日志链路吞吐为基线的 **2.99倍**，总耗时减少 **66.6%**。64条/批直接写入约1754→275ms。所有试验验证持久化记录完整性和聚合结果。

逐条输入没有批量优势：单条/批的direct约1781→1955ms，增加约9.8%，主要为批次管理和文件身份跟踪开销。收益依赖生产者一次提交多条；没有人为延长日志等待来凑批。不能据此宣称整个仿真或GPU吞吐加速3倍；服务器部署和端到端仿真A/B尚未执行。

## 复现

```bash
PYTHONPATH=. python scripts/benchmark_log_storage.py --production \
  --records 65536 --repeats 3 \
  --output docs/changes/evidence/common-performance/production-batch-jsonl.json
```

[原始结果](evidence/common-performance/production-batch-jsonl.json) · [存储方案比较](log-storage-benchmark-2026-09-29.zh-CN.md)
