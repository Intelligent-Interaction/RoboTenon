# 框架独立审计：易用性、配置语法与性能

[English](framework-audit-2026-09-21.md) · [**简体中文**](framework-audit-2026-09-21.zh-CN.md)

日期：2026-09-21。对象为当前工作区，包括此前已完成但尚未提交的修复。审计读取了配置、CLI、预检、Graph、实验管理和观测查询代码，并用临时数据完成 8 组复现；没有修改框架实现。

本文是修复前的审计快照。A01–A08 的后续代码变更记录见[框架审计修复记录](framework-audit-repairs-2026-09-21.zh-CN.md)；本文中的复现结果和测试数字均对应修复前版本。

当前优先方向是让配置错误及早暴露、统一运行与预检的语义，以及减少 Graph 内重复处理结果的开销。历史数据路径也有上一轮修复未覆盖的边界。

## 结论与优先级

| 编号 | 优先级 | 发现 | 受影响场景 |
| --- | --- | --- | --- |
| A01 | 高 | 配置未知字段被接受，拼写错误可能静默回落默认值 | 写 Graph、禁用 Tool、修改步数限制 |
| A02 | 高 | 批量 CLI 接受但丢弃 `--graph-id` | 从单任务命令改为批量运行，切换工作流 |
| A03 | 高 | 预检与真实运行校验不一致 | 批量覆盖参数、模型端口和输出路径配置 |
| A04 | 高 | 大旧摘要会在索引自动恢复之前阻断查询 | 升级旧实验目录、索引丢失后恢复 |
| A05 | 高频工作流优先 | 无关的大节点输出显著放大 Graph 调度开销 | 高频循环、较大的规划结果或结构化观测 |
| A06 | 中 | 观测历史先解码整个窗口再抽样 | 长时段回放、按间隔抽取观测 |
| A07 | 中 | Metrics 文件替换后可能继续使用旧偏移 | 日志轮转、替换指标文件、长期运行 |
| A08 | 使用旧 API 时处理 | 旧 `/api/runs` 的 limit 没有限制扫描量 | 历史 manifest 多，外部脚本读取旧接口 |

## 已确认的行为与建议

### A01：配置语法不能及时发现拼写错误

配置类和 Graph 类继承的 `RoboModel` 使用 `extra="allow"`。部分 route/loop 子结构另有未知字段检查，但整份配置没有统一的严格边界。

复现中，`max_step: 2` 被接受，实际 `max_steps` 仍为 1000；`inptus` 被接受，实际 `inputs` 仍为 null；`enabeld: false` 被接受，Tool 实际仍为 enabled。用户看到配置加载成功，很容易误以为这些设置已经生效。

建议为框架配置建立独立的严格模型，兼容别名先统一转换，未知字段报出 JSON 路径和相近字段建议。插件的自由参数仍保留在 `params` 等明确扩展区；不能简单把所有运行契约一律改成禁止扩展。

依据：[schemas.py](../../robotenon/schemas.py)、[配置模型](../../robotenon/runtime/config.py)、[Graph 定义](../../robotenon/workflow/definition.py)。复现键：`config_typos`。

### A02：相同 CLI 参数在单任务与批量模式下含义不同

解析器接受 `--batch --graph-id alternate`。单任务请求和预检请求保存了这个 graph_id，`experiment_request_from_args` 构造的批量请求却没有该字段；批量入口实际从配置选择 Graph。因此用户指定的工作流可能没有被批量执行采用，预检与运行还可能针对不同的 Graph。

另一个入口差异也已复现：`robosys web --max-queries 4` 返回参数错误，而 `robosys-web` / `python -m robosys.web.server` 的解析器支持这个参数。

建议统一请求对象与参数声明，明确哪些参数属于单任务、批量或两者。不能支持的模式组合应在解析时拒绝；支持的覆盖值必须进入有效配置和配置快照。Web 入口应共用一套解析逻辑。

依据：[runner_cli.py](../../robotenon/runtime/runner_cli.py)、[批量入口](../../robotenon/runtime/experiment.py)、[公共 CLI](../../robotenon/cli.py)。复现键：`cli_mode`，验证到请求构造层，没有运行真实环境。

### A03：预检显示通过，但运行已经注定失败

复现了三种情况：

- 不存在的 TaskAdapter 覆盖目标：`--preflight --batch` 返回 ok 和退出码 0；真实批量入口立即报 `unknown task adapter override targets`。
- 模型端口 70000：确定的端口范围错误被归为 deferred，整体仍然 ok。
- 输出目录的已存在祖先实际上是普通文件：只检查可写性，仍报告 ok。

预检不应初始化模型或仿真，但可以复用真实运行前的纯配置解析与约束检查。建议按单任务/批量模式构建同一份有效请求，再检查参数目标、文件类型、端口范围、Graph 和组件引用。区分“确定非法”“外部资源尚未就绪”“当前无法检查”。

依据：[预检入口](../../robotenon/runtime/runner_cli.py)、[preflight.py](../../robotenon/runtime/preflight.py)。复现键：`preflight`。

### A04：旧摘要大小限制阻断自动恢复

在一个资源已释放、有运行锁和合法单任务 JSON 的已结束实验中，构造大于 16 MiB 的旧 summary，并移除 episode 索引。访问实验时，`_refresh_job` 先读取旧 summary，被大小限制拒绝；位于后面的 `_queue_index_recovery` 没有执行。复现中恢复队列为空，`index_recovery` 也没有设置。

这会使受影响实验详情报错；该实验出现在列表页时，页面查询也可能失败。手动维护 CLI 是现有绕行入口，但自动恢复承诺没有覆盖这条路径。

建议先判断缺索引/脏索引并排队恢复，在恢复期间返回最小元数据和明确状态。旧 summary 太大应成为单个实验的诊断信息，避免阻断整个列表。

依据：[旧摘要读取与恢复调度](../../robotenon/web/experiments.py)，当前关键位置为 710–715、799 行。复现键：`oversized_summary`。

### A05：Graph 对未使用的数据也重复构造快照

用一个生产节点和一个循环节点测试：循环节点显式声明空 inputs，执行 40 次，只读自身的 iteration；生产节点另返回一份循环未使用的整数列表。关闭 checkpoint，没有模型、环境和磁盘写入。三组配对样本的中位数如下：

| 输入差异 | 40 次循环及生产节点总耗时 | 循环启动间隔 p95 |
| --- | ---: | ---: |
| 上游额外输出为空列表 | 12.51 ms | 0.58 ms |
| 上游额外输出含 10000 个整数，JSON 大小 58890 字节 | 705.55 ms | 21.72 ms |

代码中每轮会序列化 outputs、冻结整份 records，启动 batch 时又建立冻结视图；即使没有新 ready 节点，也会先完成部分快照构造。这里明确存在随历史节点输出体积增长的调度成本。上述数字是本机合成测试，不是机器人动作延迟，也不能当作优化后必然获得的加速比。

建议复用按版本发布的不可变结果，未变化记录结构共享，缓存冻结视图，并尽早跳过没有 ready 节点的快照工作。大数组优先使用 Artifact/ObservationRef。必须保留并行隔离、同批次读快照和 checkpoint 一致性，不能通过共享可变字典来换速度。

依据：[scheduler.py](../../robotenon/workflow/scheduler.py)、[workspace.py](../../robotenon/contracts/workspace.py)、[节点发布](../../robotenon/workflow/runtime.py)。复现键：`graph_payload`。

### A06：观测历史的实际读取量是 length × interval

查询返回 50 条、间隔 100 时，当前实现先读取并解码 5000 条 JSON，再在 Python 中抽样。服务端分别允许 length 和 interval 最大为 1000，因此一个请求最多可能先处理 100 万条历史记录。最终 4 MiB 响应限制发生在这之后，不能限制前面的读取和解析成本。

建议把抽样和分页推进到数据库查询层，同时限制总扫描量、解码字节数和查询时间；响应附带明确的截断与续查信息。

依据：[observation_history](../../robotenon/web/query.py)、[HTTP 参数上限](../../robotenon/web/server.py)。复现键：`observation_window`；实际测试 5000 行，没有生成百万行负载。

### A07：Metrics 轮转只按文件大小识别

`reload` 仅在文件小于旧偏移时重置读位置。复现先读入 gauge=1，再用相同长度、gauge=2 的新文件原子替换原路径；文件身份已改变，但查询仍返回 1。新文件比旧文件大时，也可能从错误偏移继续读，漏掉新文件前部。

建议记录打开文件的设备号、inode 或显式代际标识；身份变化时重建读取状态。将文件替换、截断和尾部未写完分别测试，防止展示过期指标。

依据：[MetricsDaemon.reload](../../robotenon/infra/metrics.py)。复现键：`metrics_rotation`。

### A08：旧运行列表仍全量遍历

`PortalData.runs(limit=1)` 在 30 个 manifest 的测试中仍读取全部 30 个，再排序并返回 1 个。成本随整个目录增长。这个发现针对旧 `/api/runs`；新 `/api/experiments` 已使用上一轮引入的 SQLite 目录。

若仍支持该接口，建议维护 manifest 索引并统一查询预算；若只是兼容入口，应明确弃用计划和调用方迁移方式。不要仅把返回 limit 当成性能保护。

依据：[PortalData.runs](../../robotenon/web/query.py)。复现键：`legacy_run_scan`。

## 中期易用性与语法建议

以下是设计建议，未宣称已有性能收益：

1. **配置辅助**：由配置模型导出 JSON Schema，提供编辑器补全；增加有效配置预览，展示最终值来自主配置、实验覆盖还是 CLI，标记默认值。
2. **Graph 编写工具**：提供检查器和格式化器，辅助补全 next/predecessors 的双向声明，定位缺失绑定、未使用参数与不可达分支。先保留 next/goto/loop 的现有语义，减少作者重复维护结构的负担。
3. **统一操作入口**：整理 run/check/inspect 命令，复用请求解析；错误返回包含字段路径、错误码和下一步操作。Portal 将索引恢复、503 重试等待与真正失败显示成不同状态；只读请求可遵循 Retry-After。
4. **维护边界**：将实验生命周期、历史查询、结果恢复从大型 manager 中逐步分离，以现有行为和故障测试约束接口，降低一次改动跨越多条路径的风险。
5. **持续验收**：把本轮边界转成回归测试，补充受支持 Python/Pydantic 组合，以及目标 GPU 上的多 Worker、长时间运行、内存/RSS/磁盘增长和动作尾延迟测量。未做这类测量前，不给出真实负载的收益保证。

建议顺序：A01/A02/A03 → A04 → A05 → A06/A07；A08 根据旧接口的实际使用量安排。上一轮明确暂缓的动作账本、Planner 校验、GPT 取消重试、Trace 旧响应四项，不计入本次新增的 8 组发现，也未在本轮修改。

## 验证与复现

- 当前全量测试：**526 通过、1 跳过、1 条已有警告**，耗时 26.70 秒；见 [pytest.log](framework-audit-2026-09-21/pytest.log)。已有测试通过不表示覆盖了上述边界。
- 8 组复现均完成；[results.json](framework-audit-2026-09-21/results.json) 保存原始结果、性能样本和源码指纹。脚本以“复现当前问题”为成功条件，不是修复后的回归断言。
- 环境：macOS ARM64，Python 3.11.10。复现脚本的运行产物均在临时目录；框架、配置、脚本与测试源文件前后 SHA-256 一致。
- 未做浏览器交互验收、真实模型/GPU/仿真/机器人运行或长期压测。Portal 发现来自服务端路径与临时数据复现，易用性设计建议未做用户研究。

```bash
python docs/changes/framework-audit-2026-09-21/reproduce.py \
  --output /tmp/robotenon-framework-audit.json
python -m pytest -o addopts= -q
```

审计附件：[复现脚本](framework-audit-2026-09-21/reproduce.py)、[执行日志](framework-audit-2026-09-21/reproduce.log)、[验证元数据](framework-audit-2026-09-21/verification.json)。
