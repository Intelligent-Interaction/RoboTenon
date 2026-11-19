# 日志存储性能对比（2026-09-29，本地）

[English](log-storage-benchmark-2026-09-29.md) · [**简体中文**](log-storage-benchmark-2026-09-29.zh-CN.md)

## 结论

不建议为写入速度把所有日志直接迁入 SQLite。优先修复公共 MetricsDaemon 的逐条 open/write/close：批量 JSONL 在本轮真实 IPC 测试中最快。SQLite 的主要附加价值是持久化结构化查询和事务；Trace 已经使用 SQLite，取消 JSONL 副本主要节省空间，大数据量写入加速不显著。

本报告记录方案比较阶段。当时未改生产存储；后续已按用户要求实现批量JSONL，见 [正式实现与验证](batch-jsonl-implementation-2026-09-29.zh-CN.md)。

## 当前代码路径

- `robotenon/infra/trace.py`：TraceDaemon 同时写 SQLite WAL 和 JSONL，每个 ingest_batch 提交一次。SQL 查询已有索引。
- `robotenon/infra/metrics.py`：MetricsDaemon.ingest_batch 循环调用 ingest，每条记录各开关一次文件。内存指标聚合和增量 JSONL 读取已存在。
- `robotenon/runtime/observability.py`：统一的模型/仿真日志服务，覆盖所有 benchmark；目前默认同时配置 trace DB、trace JSONL、metrics JSONL。
- episode 汇总已有 `episodes.sqlite3`，迁移不能重复计算这部分收益。stdout/stderr 文本、图像和视频本次没有改成数据库。

## 方法与边界

运行平台：macOS-27.0-arm64-arm-64bit, Python 3.11.10。

- 两种数据规模：8192、65536 条，分别重复3轮，报告中位数。模式顺序固定随机打乱。
- 使用生产 TraceSpan/MetricSample 结构的合成数据，不是服务器日志回放。128个逻辑episode，metrics覆盖counter/histogram，trace属性含512字符负载。
- direct 覆盖每批1/64/256条。真实 IPC 使用现有 IngestServer 和 UnixSocketMetricsSink，128个逻辑生产者、最多32条活跃连接、64条/请求；没有绕过现有64连接准入上限，也不是128个并发仿真。
- Metrics 原型保留生产校验、序列化、内存聚合，只拦截落盘并在批次末尾集中写入；SQLite原型含episode索引。Trace SQLite-only 使用现有构造参数，无生产代码改动。
- 所有成功试验逐条核对metrics持久化记录，验证条数、顺序或时间排序结果及聚合值；trace核对总数、最新属性和episode查询数量。并发重排允许，丢失/重复不允许。
- 当前 JSONL 是 flush-only，不执行fsync。SQLite WAL FULL/NORMAL 分别测量，不宣称其断电持久性相同；未做断电/进程崩溃恢复实验。NORMAL不是简单的无风险加速开关。
- 写入计时不含建库和测试数据生成；IPC包含传输、解析、校验和落盘响应。磁盘尺寸为关闭数据库后的总文件大小，不是峰值。
- 测试为本地macOS存储子系统结果，不代表Linux服务器GPU仿真吞吐，更不代表模型推理加速。

## 65536条 metrics：真实 Unix socket 路径

| 模式 | 总耗时 | 相对当前吞吐 | 请求P95 |
|---|---:|---:|---:|
| 当前逐条 JSONL | 3.448 s | 1.00× | 129.0 ms |
| 批量 JSONL | 1.207 s | 2.86× | 46.8 ms |
| 批量 SQLite WAL FULL | 1.442 s | 2.39× | 64.1 ms |
| 批量 SQLite WAL NORMAL | 1.533 s | 2.25× | 55.1 ms |

批量 JSONL 比 SQLite FULL 在本轮 IPC 中快约16%。FULL/NORMAL 的小幅顺序差异不足以得出普遍性能排序；两种模式应按持久性要求选择。

### 写入与历史查询

64条/批的 direct 中位数：

| 模式 | 写入 | 按episode读取历史记录 | 关闭后磁盘占用 |
|---|---:|---:|---:|
| 当前JSONL | 1788.9 ms | 128.14 ms | 23.83 MiB |
| 批量JSONL | 292.5 ms | 128.61 ms | 23.83 MiB |
| SQLite FULL | 732.9 ms | 1.49 ms | 27.09 MiB |
| SQLite NORMAL | 674.0 ms | 1.51 ms | 27.09 MiB |

这里的历史查询是 JSONL 全档筛选与 SQLite 索引筛选的比较；当前 Portal metrics 的内存聚合/增量加载不是每次全档扫描，不能把该查询倍数直接称为 Portal 加速倍数。SQLite metrics 也没有节省空间，索引与页结构让其略大。

## Trace：已有SQLite，取消JSONL双写

65536条、64条/批：双写约4246 ms，SQLite-only约4224 ms，差异约0.5%，在噪声范围内；文件总量由143.00 MiB降至79.64 MiB，减少约44.3%。8192条的小数据测试写入减少约15.5%，说明收益依赖规模/索引开销，不能承诺固定速度提升。查询基本不变，因为两者原本都查SQLite。

## 建议顺序（所有benchmark共用）

1. 先把公共 metrics 写入改成一次批量校验/序列化后的批量append，保持JSONL与现有Portal、导出器兼容。预期收益来自减少文件操作和锁粒度。
2. 将trace JSONL副本改为可选导出，SQLite保持查询主存储；同步明确重放、删除episode、日志轮转和导出语义。主要收益是减少空间与重复序列化。
3. 若需要复杂历史筛选，再增加metrics SQLite后端：集中写入服务、批量事务、WAL、索引和版本迁移。同步修改Portal查询、重启聚合恢复和导出，不能只改写入端。
4. 保留进程stdout/stderr便于启动失败排障；不把图像、视频大对象塞入日志数据库。

没有测量完整仿真A/B，不能把日志吞吐2–3倍换算成episode吞吐2–3倍。当前已启用summary trace时，实际收益还取决于日志在总耗时中的占比。

## 复现与原始结果

```bash
PYTHONPATH=. python scripts/benchmark_log_storage.py \
  --records 65536 --repeats 3 \
  --output docs/changes/evidence/common-performance/log-storage-local-large.json
```

- [基准脚本](../../scripts/benchmark_log_storage.py)
- [8192条原始结果](evidence/common-performance/log-storage-local.json)
- [65536条原始结果](evidence/common-performance/log-storage-local-large.json)
