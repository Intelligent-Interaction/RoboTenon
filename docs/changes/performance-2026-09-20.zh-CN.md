# RoboTenon 性能与可用性优化验证（2026-09-20）

[English](performance-2026-09-20.md) · [**简体中文**](performance-2026-09-20.zh-CN.md)

本轮在正确性修复 R01–R11 的基础上实施了六项优化。保留配置快照、运行锁、PID 身份校验、Worker 故障隔离和资源释放检查。没有启动真实机器人或模型服务，没有部署到远端，也没有清理用户已有实验文件。

## 实现结果

- **后台停止**：HTTP 停止立即返回 `202 + pending=true`；后台执行终止、宽限等待、子进程清理和资源核验，全局锁不参与进程等待。重复请求复用当前操作，失败保持 `stop_failed` 并阻止队列复用，Portal 重启后继续已授权的清理。界面分别展示等待运行器退出、清理子进程、强制清理、释放完成或需要人工处理。
- **增量结果**：逐任务 JSON 仍是完整结果来源；新增 `episodes.sqlite3` 查询索引。任务状态切换只更新本任务、计数和当前状态；初始及控制操作只写紧凑摘要，结束时生成完整 `summary.json` 和按任务目录统计的 `stats.json`。正常批次累计摘要从每个任务重写改为初始化一次、结束一次。调度器也改为任务完成时立即补充队列，保留 100 ms 控制命令检查上限。
- **有界查询**：实验列表仅刷新请求页；episode 使用 SQLite 索引；Trace 限时、限行、限字节，超限有明确错误或下一页位置。实验列表、episode、Trace 页面支持翻页与时间范围，SQL 支持下一页；批量控制直接校验索引身份，最多 500 个目标，全实验操作保持独立入口。页面区分本页任务数和全部成功数。
- **资源增长**：JSON 缓存使用 LRU，最多 256 项且估算容量不超过 32 MiB；单个 JSON 读取最多 16 MiB。配置缓存最多 64 项，观测路径映射最多 4096 项，视频处理锁使用弱引用。终态进程确认释放后缓存确认结果，避免历史列表反复扫描进程表；重跑清除这一确认。
- **启动诊断**：CLI、Portal 提交和“启动检查”使用共同的配置、Graph 输出、GPU 分配、模型路径、可执行文件、磁盘、端口诊断。可选 GPU 探测限时 2 秒；显式 IP 模型端点 TCP 探测限时 250 ms。远程模型标识、DNS 端点或本机不可探测的 GPU 标为待运行时确认。模型协议就绪仍由原有 Worker 启动检查负责。界面依据 live 快照区分排队、加载、执行、清理、人工处理。
- **Worker 通知**：先测量 1 / 4 / 8 个真实空闲 Worker，再将运行中双方 20 ms 轮询改为 Pipe 可读通知。健康检查每 250 ms、心跳每 1 秒独立计时。取消时移除读监听；备用事件循环使用有期限的线程 `poll`。退出清理阶段仍保留原有短期轮询和强制回收逻辑。

## 测量结果

基线来自性能改动前的隔离代码快照，而非 Git HEAD；工作区原有未提交修改包含在基线中。环境为本机 macOS / Apple Silicon、Python 3.11。主基准前后各独立运行 3 次，表中为中位数；完整样本和范围见 `performance-2026-09-20/comparison.json` 与 `before-1..3.json`、`after-1..3.json`。

工作负载：300 个假任务、8 个并发槽位、每任务等待 1 ms；200 次异步假驱动动作；300 条历史记录与一个模拟清理耗时 500 ms 的实验；3 轮各 400 个 8 KiB JSON；20,000 条 Trace 样本。所有数据写入临时目录。

| 指标 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 假任务吞吐量（任务/秒） | 76.71 | 407.10 |
| 300 任务耗时（秒） | 3.911 | 0.737 |
| JSON 序列化写入总字节¹ | 131,290,902 | 4,164,236 |
| summary.json 写入次数 | 302 | 2 |
| summary.json 累计写入字节 | 68,821,830 | 251,185 |
| 停止请求响应（ms） | 513.131 | 1.374 |
| 停止期间列表读取 p95（ms）² | 545.368 | 0.739 |
| 列表返回字节（请求 limit=25） | 79,318 | 7,282 |
| 列表返回条数 | 301 | 25 |
| 假驱动动作 p95（ms） | 1.386 | 1.303 |
| 假驱动动作 p99（ms） | 1.462 | 1.375 |
| 300 任务最终保留字节³ | 810,269 | 924,589 |

¹ 只统计 `write_json` 写出的 JSON 字节，不包含 SQLite/WAL、文件系统日志或 fsync 的物理写入量。吞吐量是增量保存与调度改动的综合结果，不能外推到 GPU 推理或机器人实测。

² 每轮 8 次串行读取，p95 使用 nearest-rank，因此会包含被原全局锁阻塞的请求。停止请求返回表示已受理，后台约 500 ms 清理仍然继续；回归测试单独验证释放前 `released=false`，不是省略了清理。

³ 新索引约 311,296 字节；最终保留量增加约 14.1%。减少累计写入与减少最终磁盘占用是不同指标。

队列剩余任务降到一半的中位时间：1.924 → 0.404 秒。假驱动动作延迟处于相同量级，此项作为未出现明显回退的检查，不宣称动作执行加速。

缓存容量试验：优化前累计 400 / 800 / 1200 项，第三轮 tracemalloc 分配约 10.86 MB；优化后保持 256 项，约 2.35 MB。临时 JSON 文件仍从 3.28 MB 增长到 9.85 MB，因为该试验不启用保留策略。这是容量回归试验，不是 24 小时或一周长稳测试。

Trace 样本由一次返回 20,000 行、5,668,890 字节，改为首次返回 1,000 行、281,890 字节并提供 `next_offset`。返回量不同，不能作为同等全量查询吞吐量对比。

真实 Worker 空闲测量使用无模型的配置，每组采样 5 秒；CPU 为所有 Worker 消耗的单核百分比（不是整机占用率）：

| Worker 数 | 优化前 CPU | 优化后 CPU |
| --- | ---: | ---: |
| 1 | 1.36% | 0.21% |
| 4 | 4.64% | 0.71% |
| 8 | 9.20% | 1.43% |

8 Worker RSS 约 399.8 → 400.5 MiB，没有因通知方式出现明显增长。实际父进程 `receive` 路径配合合成 Pipe 发送端的 p95：18.067 → 0.095 ms；p99：18.162 → 0.116 ms。此数据不代表模型推理或端到端动作延迟。

## 接口与运行方式

| 接口 | 分页 / 时间范围 | 默认边界 |
| --- | --- | --- |
| `GET /api/experiments` | `limit, offset, since, until, search` | 默认 100、最多 500 条；offset 最多 100000；历史刷新预算 1 秒 |
| `GET /api/experiments/<id>/episodes` | `limit, offset, since, until` | 默认 100、最多 500 条；按记录更新时间过滤 |
| `GET /api/traces`、`GET /api/experiments/<id>/traces` | 同上，可按 episode / attempt 筛选 | 500 ms、3 MiB 结果预算 |
| `POST /api/trace/sql` | `query, params, limit, offset, job_id` | 默认 1000、最多 10000 行；500 ms、3 MiB；建议 SQL 显式 `ORDER BY` |
| `POST /api/preflight` | `config, overrides` | 只读诊断，不创建实验 |

时间参数用带时区的 ISO 8601；达到 offset 上限应缩小时间范围。分页响应含 `next_offset`，Trace 还含 `truncated`。HTTP JSON 总大小限制 4 MiB，大小超限返回 413，超时返回 504。未知实验返回 404。Python `TraceQuery.sql()` 超预算时抛错，避免把截断结果当成完整结果；需要分页的消费者使用 `sql_page()`。

```bash
# 启动前只读检查；发现 error 时返回非零退出码
Tenon run --preflight --config configs/robotenon_xr365.json

# 先预览保留策略，不删除文件
python scripts/maintain_experiments.py --output-root data/experiments \
  --policy configs/operations/retention-policy.json

# 明确启用后按策略清理终态实验的大文件
python scripts/maintain_experiments.py --output-root data/experiments \
  --policy configs/operations/retention-policy.json --apply

# Portal 持续运行时，每小时按该策略清理
Tenon web --retention-policy configs/operations/retention-policy.json

# 给已有、可验证已停止的实验重建索引
python scripts/maintain_experiments.py --index-run data/experiments/EXPERIMENT_ID
```

默认策略：普通日志 30 天、Trace 30 天、视频 7 天；失败实验这些文件至少保留 90 天。保留配置、单任务结果、汇总和运行锁。清理检查终态、独占运行锁、待重跑标记、存活进程和路径边界，不跟随符号链接；没有可验证运行锁的旧实验会跳过。单次扫描有时间和条目预算，报告 `truncated` 时需继续运行维护命令。**自动清理默认未启用，本轮未删除用户历史数据。**

旧版小摘要继续可读；超过 16 MiB 的旧 JSON 会返回明确的迁移提示。可验证已终止的旧实验可通过索引重建命令迁移。`stats.json` 现在统计所在任务目录，跨任务完整统计仍在 `summary.json`。预检报告是诊断，提交仍保留原有执行和就绪验证流程；不会把 deferred 项当作已经通过的服务就绪检查。

## 验证与边界

- Python 全量：**506 passed、1 skipped、1 warning**；跳过项为需要 Linux 的原生崩溃测试，警告为原有不可达默认路由提示。
- 前端 Node：**20 passed**；JS 语法检查、Python 编译、相关静态错误检查及新增文件 lint 通过。
- 新增 26 个回归用例覆盖并发停止、清理失败、重启恢复、运行中索引可读、原地重跑不改变其他结果、分页/时间校验、Trace 超时/大小预算、LRU、保留策略与持有锁/软链接保护、只读预检、Pipe 通知取消/EOF 和加载阶段。
- 隔离本机 HTTP + 浏览器：125 个实验翻页和搜索正常；230 个 episode 分为 100 / 100 / 30，末页禁用下一页，前后切换正常；启动检查不创建实验，Trace SQL 可执行，控制台无错误。
- 未做真实 GPU / 机器人 / 长时间稳定性压测。`jobs.json` 元数据仍整体载入内存；保留策略不删除结果元数据，也不替代磁盘总配额。真实业务吞吐、故障时模型服务就绪时间、24 小时内存/磁盘曲线仍需在目标环境验收。

复现命令（在要测量的代码目录执行）：

```bash
python scripts/benchmark_runtime.py --output /tmp/runtime-benchmark.json
python scripts/benchmark_workers.py --idle-seconds 5 --output /tmp/worker-benchmark.json
python -m pytest -o addopts='' -q
node --test tests/test_graph_routes.js tests/test_trajectory_media.js
```

结果目录：[performance-2026-09-20](performance-2026-09-20)。其中 `verification.json` 记录变更文件与哈希，`baseline-manifest.json` 记录优化前文件，`performance-only.patch` 只包含本轮相对于正确性修复后快照的代码差异；测试日志、浏览器检查、原始样本和比较摘要均已保存。
