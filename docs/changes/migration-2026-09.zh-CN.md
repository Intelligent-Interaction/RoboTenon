# 2026-09 架构修复与迁移说明

[English](migration-2026-09.md) · [**简体中文**](migration-2026-09.zh-CN.md)

## 2026-09-23：predecessor OR 组

- `predecessors` 新增两层分组语法：外层组之间是 AND，内层成员之间是 OR。`["a", ["b", "c"]]` 表示 `a AND (b OR c)`。
- 原一维列表保持严格 AND，不需要迁移。空组、三层嵌套、重复成员和未知节点会在加载时拒绝。
- 编译器仍把全部组成员视为依赖边做引用与环检查；checkpoint 保存实际到达成员。已满足 OR 组的其他成员不再出现在 waiting diagnostics 中。
- `goto` 继续绕过 predecessor join。旧 `edges` 自动转换仍只生成一维 AND 前驱。

## 2026-09-20：接入与基础设施插件化

- 新增通用 environment.parameters、TaskCase.parameters.environment_parameters、EpisodeRequest.environment_parameters；对象递归合并，列表替换。既有规范字段最后覆盖。
- ExperimentRequest 支持 task_adapter_overrides/environment_parameters；CLI 对应两个 JSON 参数入口，split 不再限制 pretrain/target。
- TaskAdapter 构造参数覆盖改为递归合并；旧浅覆盖依赖需要检查嵌套默认值是否应保留。历史快捷参数由 provider 的 cli_overrides 声明接收。
- 新增执行后端 registry，单任务与批量按能力装配；现有 in_process/process 配置可继续使用。
- 安全、checkpoint、动作去重和观测存储可注册；require_shared 校验 shared 能力。remote 观测后端现在创建真实远程客户端，须提供 params.socket_path，原先误用 remote 作为本地缓存的配置应改回 hybrid。
- RPC 客户端/服务端新增传输插件配置，原 UDS 构造方式不变；Body 两条连接均继承传输配置。
- ObservationFrame.modality/producer 与 ObservationChannel.modality 接受非空字符串；明确声明的模态被保留。role、capture_policy 和安全语义保持原契约。
- 应用装配统一失败清理，配置创建的 context/safety stores 随应用关闭；外部注入实例不转移所有权。

完整配置及兼容别名见[基础设施插件](../infrastructure-plugins.zh-CN.md)，实际验证见[验证记录](verification-2026-09.zh-CN.md)。


## 2026-09-20：next / goto / loop 独立语法

- 旧普通 goto → next；目标 predecessors 保留。
- 旧任意目标 loop（包括原 kind: loop）→ goto，删除 kind。
- 新 loop 只表示当前节点重复：true 或 {"when": "条件"}，不接受 node 或 kind。
- 旧 edges 的普通边仍可用；带 kind 的回边须迁移到节点 goto，并将普通边迁移为 next/predecessors。
- default 保持普通后继语义；仅在三个操作都不匹配时生效。
- 前驱 next 真正触发时，旧自循环调用失效；普通批次与自循环独立调度。ctx 读取快照与私有工作区保留，失效和提交串行化。
- 图指纹新增 routing_version=5，旧在途 checkpoint 必须显式重启；不按新语义静默续跑。
- 现有 configs/graphs.json、configs/robotenon_xr365_graphs.json 已迁移。外部保存的完整实验配置需同样迁移。

完整说明：[Workflow](../workflow.zh-CN.md)、[节点私有上下文](../node-private-context.zh-CN.md)。


## 2026-09-16：基础架构修复

- 修复 P3/P4 同名信号锁存被覆盖，内存与 SQLite 使用相同合并规则。
- 修复多节点循环使用上一轮完成记录、并行子图误判递归、在途 checkpoint 不能继续执行。
- Body RPC 分离执行/控制连接；总 deadline 包含锁等待；取消后清理连接并请求远端停止。
- edges、predecessors/goto 共用 WaveScheduler。
- 拆出 contracts、storage、compiler、conditions、scheduler 和 operations。
- 增加 NodeResult/GraphModule、显式 patch 冲突检查、配置预检与 checkpoint 版本/指纹。
- 将服务请求缩小为 ServiceContext，移除服务端对工作流实现的导入。
- 懒导出和延迟资源装配；模块取消回滚、服务启动失败清理、SQLite 短连接关闭。
- 统一严格 JSON codec：checkpoint/ctx/参数哈希和 IPC 不再静默字符串化未知对象，并拒绝 NaN/Infinity；Monitor 自动观测包装的临时单例绑定可安全恢复。
- 拆出 Web 静态资源、查询层和 Grafana 启动模块；静态资源纳入 wheel。
- 增加故障回归、依赖边界检查与 CI 配置。
- 恢复独立的常驻 ToolRuntime：模型、Memory、Skill 和自定义能力不再伪装成 Module；新增 `tools_configs.json`、ToolRegistry 和 `ctx.call_tool()`。
- 增加统一 Parameter 入口：主 `config.json` 负责引用 modules/graphs/tools/monitors 子配置，实验 JSON 深度覆盖；所有图 Module 只接收 `ctx`，参数使用 `ctx.params`，环境通过进程单例 `robotenon.envs.environment` 调用。Monitor 通过独立注册表支持 resident/request 双模式。
- Monitor 配置兼容 `monitor_config`、`monitors_config` 两种主配置键，以及实例上的 `mode: "both"`、`resident/request` 布尔写法；推荐统一使用 `modes: ["resident", "request"]`。
- Body 与 EnvironmentAdapter 现在也有独立注册表和主配置子清单：新增 `body_configs.json`、`envs_configs.json`，分别使用 `@register_body` / `@register_environment`；Body 由 `initialize_body_service` 启动，EnvironmentAdapter 由 `initialize_environment_adapter` 连接，Session 仍按 Run 创建。

## 兼容性变化

| 旧行为 | 新行为 / 迁移方式 |
| --- | --- |
| backend 选择 LangGraph 或 fallback | 名称仍接受，内部只有一个调度器；不再构造 StateGraph |
| import robotenon 时自动注册全部 builtin | 装配函数负责注册；查询前可显式 load_builtin_components() |
| 导入时创建环境变量指定的数据库 | 在装配或默认 facade 首次使用时创建 |
| 仅有 run 的模块可能收到 ModuleContext | 始终收到 GraphContext；同时有 invoke 的模块，call 优先 invoke |
| edges 与 predecessors/goto 混用可能隐式忽略部分配置 | 编译器明确拒绝，先迁移为单一格式 |
| 旧 checkpoint 保存 live graph_stack | 不再持久化 live stack；旧在途快照拒绝自动恢复 |
| 同 node_id 输出可能在子图间覆盖 | 旧 outputs 视图保留，新增 scoped_outputs 区分完整调用路径 |
| 未知 checkpoint 对象转 str、损坏模型丢为 None | 明确报错，插件需提供 JSON 可序列化数据 |
| 远程 reset/check 接收整个 GraphContext | 接收 ServiceContext；调度字段不再跨服务传递 |
| Tool、Memory、Skill 作为 Module 注册 | 迁移到 `@register_tool` + `tools_configs.json`；Module 通过 `ctx.call_tool()` 使用常驻实例 |
| Module `run(ctx, env, params)` | 改为 `run(ctx)`；使用 `ctx.params` 读取参数，并从 `robotenon.envs` 导入 `environment` 单例调用环境 |

旧导入如 `from robotenon.runtime.safety import SQLiteSafetyStateStore`、`from robotenon.core.context import SQLiteContextStore`、`from robotenon.services.action_ledger import SQLiteActionExecutionStore` 继续可用。新实现分别位于 storage/safety.py、storage/context.py、storage/actions.py。

## checkpoint 的承诺与边界

这是“保存业务状态和执行游标，继续未完成节点”，不是外部副作用的 exactly-once 协议。

- 使用相同 session_id 的空 GraphContext 恢复。initial_values 在加载快照后合并。
- 已提交的节点跳过；未提交的节点可能重新执行。动作应复用已持久化的 action_id/idempotency_key。
- 恢复前验证图与模块配置指纹；未通过就拒绝执行，不从 entry 偷偷重放。
- 已完成 Run 再次显式调用 run 会开启一次新遍历；业务 values 保留。
- 旧的在途快照需由调用方决定重置或对账，不能直接复制旧 graph_stack。
- 物理动作与 ack 落盘之间仍需设备端幂等或状态查询来对账。

## 并行模块

共享 ctx 身份保持兼容。ExecutionFrame 隔离调度元数据，不使任意 dict 写入自动具备事务性。

新插件优先返回 NodeResult，显式 patch 在同一 wave 必须单写者。直接写 ctx.values、返回普通 dict 或在模块中发起外部副作用的旧插件，需要自己遵守不冲突/幂等契约。`GraphContext.history` 与 `scoped_outputs` 现在默认保留最近 2048 条/项，可通过构造参数调整；淘汰计数分别记录在 `history_dropped`、`scoped_outputs_dropped`。Trace/Recorder 是长期日志的持久化出口；有界缓冲与故障可能导致事件缺失。

`RunCheckpoint` 是公开的版本化 DTO（`robotenon.contracts.checkpoint`）。它把 `checkpoint_version`、图指纹、CAS revision、执行游标和 JSON 状态明确分开；调用 `ctx.to_checkpoint()` 可导出，`GraphContext.from_checkpoint(...)` 可恢复。运行中的 task-local `graph_stack` 不会写入快照，取消时运行时会尽力保存最近游标。


## 验证入口

```bash
python -m pytest
Tenon modules-check
Tenon tools-check
Tenon graphs-check
Tenon run-fake --output /tmp/robotenon-smoke
python -m ruff check --select F robotenon
```

故障回归位于 tests/test_architecture_regressions.py。本机 UDS 集成测试需要允许绑定临时 socket；CI 在 Linux 主机运行。docs/changes/audit-2026-09-15 保留修复前审计与哈希，旧 reproduce.py 的断言不再代表修复后的期望。

此次未连接真实机器人，也未给出 100Hz 控制路径的性能保证；SQLite 锁竞争与具体 SDK 的尾延迟仍需在部署环境测量。

## 2026-09-18：统一执行与观测迁移

| 需要核对的内容 | 当前约定 |
| --- | --- |
| 并行配置 | 使用 `parameters.runtime.execution`；`backend` 为注册名（内置 `in_process` 或 `process`），并发数为 `max_workers` |
| 早期方案参数 | `runtime.parallel.enabled`、`persistent_gpu_workers`、`max_in_flight` 未实现，不要写入配置 |
| 单任务与真机 | 共用 WorkerRuntime 生命周期，由配置决定资源与执行后端，没有旧配置专用分支 |
| Tool 缓存 | 跨任务复用权重/连接，但用 `end_session(session_id)` 清理任务引用和会话状态 |
| 批量记忆 | 默认 snapshot 并确定性归并；顺序可见写入选 sequential 且 max_workers=1 |
| 产物路径 | 进程执行按 attempt 隔离；观测和 safety 资源随任务根目录变化，外部服务必须使用对应路径 |
| Web 数据源 | 显式任务库或共享库，取消数据库猜测和 JSONL 回填；媒体索引另行配置 |
| 自动 Web | 只读观测页面，生命周期随运行，不等于完整实验管理 |
| 重试 | 仅进程丢失等执行故障按 max_retries 重试，普通任务错误不自动重试 |
| 恢复 | 没有 Coordinator 崩溃接管；Graph ContextStore 需显式启用 |

实验/运行/episode/attempt 等身份贯穿 Trace 和 Metrics；旧数据缺少身份时，按实验筛选可能不可用，不自动推断旧数据身份。Metrics 仍是 JSONL，不能把“共享 SQL”理解为已统一存储全部指标与媒体。

迁移时先验证 Fake 与连续任务清理，再核对数据源和配置，最后在实际依赖齐备的环境验证仿真/硬件。模板不提供厂商 driver，资源锁不替代硬件保护。具体命令、默认值与限制见[执行与统一观测](../execution-and-observability.zh-CN.md)。

## 2026-09-18：P1 审计修复后的迁移

- 关闭 Monitor 不再忽略已配置的共享安全状态。已有 P3/P4 锁存可能使任务正确进入安全停止；不要为了跑通任务自动清空安全库。
- 启用 Monitor 后，清单中的插件会实际运行。原先未接入的检查可能开始阻止动作；这是修复后的预期行为。
- 安全存储新增 `scope: "shared" | "task"`，默认 shared 保留显式路径。仿真模板显式选择 task，设备共享安全状态使用 shared。
- 完整 Portal 的控制请求新增 Bearer 令牌和来源校验。浏览器从终端启动链接自动读取令牌；旧 API 客户端需添加 Authorization 与 JSON Content-Type。额外域名使用 `--allowed-host`。
- 已启动的旧服务不会因文件同步自动加载修复；下次启动使用新实现。只读查询权限不等于控制权限。

### 周期循环与调用来源

`goto` 允许首次调用目标并跳过前驱等待。`loop` 新增可选 `every: "20ms"`（支持 ms/s），省略时保持立即重复。`ctx.trigger` 暴露 entry/next/goto/loop，可用于 when 避免自循环重复发起外部链路。无条件路由与 default 共存会发出不可达警告。周期在进程恢复时重新计时，不补跑错过的 tick。

### 前驱完成与图生命周期

next/default 保持 AND 汇合：每个 predecessors 都必须完成并实际选中目标。执行期间的重复请求保留一次后续调用。loop 到期前重查条件，使用与新调用相同的读取快照。END 仅结束分支，新增 system 操作 graph.stop 停止当前图（在子图内只停止子图）。路由代次、前驱凭据和请求进入 checkpoint；旧指纹的进行中任务必须显式重启。

### 调用合并与汇合诊断

完整 next 与同目标 goto 在启动前合并，以 ctx.trigger=next 执行；部分前驱到达不阻塞 goto。GraphResult.routing_diagnostics 和 episode summary 返回未释放汇合的缺失前驱与原因，checkpoint 保留诊断。推荐条件边统一使用 node/when 对象格式，旧写法保持兼容。loop.when 的定时重查保留安排循环时的调用来源，实际自循环执行来源为 loop。

### 条件与周期观测

加载时检查 results/result_versions 的静态节点名；未知节点需修正，动态业务字段不做静态推断。GraphResult 与 episode summary 新增 routing_events、routing_events_dropped、loop_metrics。判断记录和周期统计接入现有 Trace/Metrics；checkpoint 中每图保留最近 200 条观测。所有配置统一按当前语法维护，不新增语法版本字段或多版本兼容。
