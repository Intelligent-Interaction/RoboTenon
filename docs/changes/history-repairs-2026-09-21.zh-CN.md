# 上一轮审计修复：历史目录与批量执行

[English](history-repairs-2026-09-21.md) · [**简体中文**](history-repairs-2026-09-21.zh-CN.md)

记录日期：2026-09-21。按本轮明确范围，只修复上一轮的五项问题。最新审计中的动作账本异常记录、Planner 输出校验、GPT 取消后重试、Trace 页面旧响应覆盖问题暂不处理；对应四个源文件与本轮开始时的 SHA-256 一致。

## 已完成的修复

| 问题与场景 | 当前行为 | 主要实现 |
| --- | --- | --- |
| 历史目录很大，清理反复从头扫描 | SQLite 保存目录位置与候选文件；实验轮流扫描，未完成时后台约 30 秒后继续。预览不推进真实断点；每轮重新核验运行锁、重跑标记、存活进程和路径边界。报告剩余磁盘空间与低水位。 | [retention.py](../../robotenon/runtime/retention.py) |
| 异常退出后 episode 索引缺失、损坏或未写完 | 访问历史实验时排队后台恢复，从单任务 JSON 重建索引、状态和汇总计数；不依赖完整 summary。恢复持有运行锁，坏源文件明确报错；失败后可修复源文件再重试。 | [result_index.py](../../robotenon/task_adapters/result_index.py)、[experiments.py](../../robotenon/web/experiments.py) |
| 大量历史实验使启动、分页和状态保存越来越重 | 实验目录迁移到 jobs.sqlite3；只更新变化行，缓存限制 128 条与 8 MiB 编码大小。调度只读取活动实验和等待队列；列表支持索引查询与游标翻页。 | [jobs.py](../../robotenon/storage/jobs.py) |
| 大结果批次持续占用内存，写盘阻塞执行循环 | 完整结果逐任务保存，内存只保留紧凑统计；一个后台写入线程、最多 16 个在途操作，复用 SQLite 连接。取消时等待已接受写入完成，异常退出保留索引未完成标记。 | [result_writer.py](../../robotenon/task_adapters/result_writer.py)、[evaluator.py](../../robotenon/task_adapters/evaluator.py) |
| 并发查询创建过多 HTTP 线程，挤占控制请求 | 默认最多 32 个连接、8 个普通请求，健康检查和停止操作另有 4 个槽位；满额返回 503 与 Retry-After。 | [http_server.py](../../robotenon/web/http_server.py)、[server.py](../../robotenon/web/server.py) |

恢复替换索引前先完整验证源记录；替换后的索引保持未完成状态，直到状态和汇总也保存成功。这样恢复本身被中断后，下次仍会重试。长等待队列也按页读取，避免缓存淘汰使刚派发实验的状态修改丢失。

## 验证

- 全量 Python：**526 通过、1 跳过**，退出码 0。有一条已有的 Graph 不可达默认分支警告。
- 本次新增故障与规模测试：**20 通过**，覆盖清理续扫与公平性、慢目录前缀重放、预览、锁保护、单行持久化、游标稳定性、长队列、索引损坏与重试、初始写入取消、写入关闭屏障、内存释放和 HTTP 容量。
- 前端现有 Node 测试：**20 通过**。本轮未修改前端脚本。
- 新增及重写模块通过 Ruff 默认检查；已有大型模块通过 E9/F63/F7/F82 检查。
- 维护 CLI 在临时数据上验证了索引恢复、清理预览和实际清理；没有操作用户历史实验。

详细结果见 [verification.json](history-repairs-2026-09-21/verification.json)、[pytest.log](history-repairs-2026-09-21/pytest.log)、[Node 日志](history-repairs-2026-09-21/node-tests.log)及 [CLI 结果](history-repairs-2026-09-21/cli-checks.json)。

## 合成性能测量

环境为本机 macOS ARM64、Python 3.11.10、本地临时目录，使用 fake 任务与驱动。以本轮开始时的完整工作区快照作为修复前版本，包含此前已经存在的未提交修改。最终采用“修复前一次、修复后一次”成对顺序运行，共三对；下表为各组三次中位数。

| 样本与指标 | 修复前 | 修复后 |
| --- | ---: | ---: |
| 2 万条历史，正常重启初始化耗时 | 175.88 ms | 1.69 ms |
| 同上，启动阶段 Python 分配量 | 64.78 MB | 0.015 MB |
| 同上，读取 25 条目录记录 | 46.38 ms | 3.11 ms |
| 同上，保存一条状态修改 | 862.47 ms | 1.45 ms |
| 100 个任务、每个结果 256 KiB、4 并发，Python 分配峰值 | 27.18 MB | 2.06 MB |
| 同一大结果批次总耗时 | 0.520 s | 0.561 s |
| 300 个 fake 任务、8 并发，吞吐量 | 388.14 个/秒 | 588.15 个/秒 |
| fake 动作延迟 p95 | 1.367 ms | 1.469 ms |
| fake 动作延迟 p99 | 1.531 ms | 2.598 ms |

大结果样本的 Python 分配峰值降低约 92%，但总耗时增加约 8%。线程交接、写入背压和取消时的完成等待有成本；这项修复不能解释为所有任务都会更快。fake 动作延迟尾部在本轮变大，样本波动也明显：修复后三次 p99 为 1.54–5.02 ms。本轮没有修改动作执行实现，这些数据不足以判断真实机器人动作延迟的变化。

历史目录样本每条附带 2 KiB 参数，列表测试替换了运行状态刷新，仅测元数据查询和序列化成本。启动测量排除一次性旧 JSON 导入；首次迁移仍须解析旧文件。内存数字来自 tracemalloc，表示 Python 分配量，不是进程 RSS，也不包含全部 SQLite 原生分配。大结果内存测试启用了 tracemalloc，普通吞吐量测试未启用，两组耗时不能直接互比。

完整样本和最大/最小值见 [comparison.json](history-repairs-2026-09-21/comparison.json)。先前非成对样本保留在 [preliminary](history-repairs-2026-09-21/preliminary/comparison.json)，不能与最终样本混用。没有执行真实模型、GPU、渲染、真机或长期运行验收。

复测入口：

```bash
python scripts/benchmark_history.py --output /tmp/history-after.json
python scripts/benchmark_runtime.py --output /tmp/runtime-after.json
python -m pytest -q tests/test_history_reliability.py
```

基准脚本根据运行时工作目录导入被测版本；比较修复前版本时，需要在对应快照目录运行相同脚本。

## 升级与使用边界

1. Portal 下次启动时自动将旧 jobs.json 一次性导入 jobs.sqlite3。旧 JSON 保留为迁移前备份，后续不再更新；读取实时实验状态的自定义脚本需改用 API 或 SQLite。当前设计面向同机本地文件系统。
2. 列表保留 offset 参数供现有页面使用，API 额外支持 next_cursor。搜索和计数受查询预算限制，仍可能随历史量增加而变慢。
3. 自动清理仍默认关闭。本次仅增加断点与磁盘报告，没有替用户启用删除。扫描时间预算是软限制；目录前缀重放和文件系统调用可能超时，但每次续扫仍会推进新条目。
4. 自动索引恢复要求实验已结束、资源已释放，并能取得运行锁；恢复不是自动续跑实验。恢复中的查询返回 503，错误原因可在 index_recovery 中查看。
5. 内存仍随 case 列表、紧凑统计和最终摘要的条数增长；单个超大结果仍需处理。8 MiB 元数据缓存预算允许独立保留一条超大记录。
6. 控制槽位保障普通请求满额时的请求接纳。总连接达到 32 个时，新控制连接也会返回 503；这不等于 CPU、文件系统和内部锁完全隔离。

配置、API 和维护命令见[运行与观测参考](../execution-and-observability.zh-CN.md#14-历史目录索引恢复与请求容量)。

本轮相对起始快照的 13 个代码、测试和配置文件变更保存为 [changes.patch](history-repairs-2026-09-21/changes.patch)，文件指纹及暂缓问题未修改的证据见 [source-manifest.json](history-repairs-2026-09-21/source-manifest.json)。文档另更新本记录、更新索引和运行参考。当前工作区原有修改已保留；未提交代码、重启现用服务或部署。
