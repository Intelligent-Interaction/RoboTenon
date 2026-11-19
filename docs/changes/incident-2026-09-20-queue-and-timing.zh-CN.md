# 2026-09-20 实验排队、调用耗时与失败原因

[English](incident-2026-09-20-queue-and-timing.md) · [**简体中文**](incident-2026-09-20-queue-and-timing.zh-CN.md)

## 现场证据

实验 `exp-20260920-104320-ca107d28` 在核查快照中已完成 25/317 个案例，16 成功、9 失败，失败率 36%，运行仍在继续，不能视为最终成功率。9 个失败案例的 attempt/resources/safety.sqlite3 均存在 Body deadline 信号。

有效参数为 action_timeout_ms=120000、chunk_duration_ms=16000、action_chunk_size=16。Body 实际取单动作、整批、绝对截止时间的最小值。CloseMicrowave 的 body_execute 从 02:47:34.127896 UTC 到 02:47:50.097815 UTC，约 15.97 秒便触发整批预算。旧错误信息却统一写成 action timeout after 120000 ms。安全停止可发生在 evaluator 再次运行前，因此旧摘要中的 reason=continue 和 env_steps=0/16 可能是上次 evaluator 的结果，不是完整的最终执行诊断。

Trace 中 routing:plan/action 是路由条件判断，例如 routing:plan 持续 0.366 毫秒；此前误入调用耗时图，并被浏览器日期精度和整数显示变成 0 ms。真正 plan/node 与 Tool span 有正常秒级耗时。

第二个历史实验 exp-20260920-095212-e1b99275 的直接启动错误是旧图校验 ValueError: edge reset->plan requires source goto or default，而不是已证实的显存冲突。提交器此前确实没有等待队列，会立即启动所有提交。

## 修改

- Portal 以持久化 waiting 状态串行调度实验；后台自动接续，重启恢复活跃 PID，支持取消等待和全局停止。单个实验内部的 8 GPU 并行不变。
- 实验概览单独统计等待数量，列表提供取消等待。
- 调用耗时图排除 routing span；原始 Trace 和路由诊断仍保留。保留亚毫秒精度，耗时小于 1 ms 显示 <1 ms，条形图使用直角。
- XR365 8 GPU 默认整批预算设为 1920000 ms，即 16 × 120000 ms；保留单动作和整批安全截止时间。
- Body 区分整批超时、单动作超时与绝对截止时间；safety_stop 摘要不再沿用 continue 原因。

## 验证与部署

本地与 model-runtime 服务器环境均通过 55 项 Python 回归，包括并发提交、等待取消、重启恢复、后台接续、真实子进程串行启动和超时分类。9 项 JavaScript 测试通过。

同步前备份：/path/to/deployment/queue-fix-20260920-110044.tar。仅重启 Portal，保留其他运行进程。

运行中的实验仍使用已加载的旧预算；新预算用于后续新建实验。Fork/重跑按设计复用历史参数，若从旧实验 Fork，需要把 policy.chunk_duration_ms 改成 1920000。未用完整 GPU 评测证明修复后的最终成功率，当前仍运行的历史实验不应作为新预算的验证。
