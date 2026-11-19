# 2026-09 修复验证记录

[English](verification-2026-09.md) · [**简体中文**](verification-2026-09.zh-CN.md)

## 2026-09-20：接入与基础设施解耦

- 本地 macOS、Python 3.11.10 完整回归：**440 passed、1 skipped、1 warning，21.00 秒**。
  warning 来自已有 Graph 路由校验用例的不可达 default 提示。
- 外部插件验证覆盖：Environment/TaskAdapter 的顺序和 spawn 多进程任务；自定义执行后端的单任务/批量入口；四类存储装配与关闭；启动失败清理；动作存储重启；自定义流传输的 Environment RPC 和 Body 独立控制连接；自定义观测模态缓存往返。
- 架构测试约束入口按能力装配，不直接导入 process 实现或按具体 backend 名称分支；观测管线依赖存储协议。
- 27 份 Markdown 的本地链接全部可解析，29 个 JSON 代码块语法通过；XML 汇报稿片段解析通过。
- 本轮新增基础设施代码、关键修改文件与架构测试的 Ruff F 规则通过；`git diff --check` 通过。

本次使用模拟插件、临时存储和本机 socket 验证，没有运行 LIBERO 仿真、GPU 模型、真实机器人或跨主机部署；未重启现有远端服务。接口细节见[基础设施插件](../infrastructure-plugins.zh-CN.md)，修复范围见[接入检查报告](plugin-extension-audit.zh-CN.md)。

## 2026-09-18 P1、Worker 模型部署与文档补充

- P1 修复完成后的完整回归：本地与服务器均为 **196 passed**。覆盖关闭 Monitor 时保留配置的安全后端、注册式 Monitor 装配、Portal 控制授权，以及未启动 Monitor 不清理同名外部心跳。
- 每 Worker 模型部署补充后的完整回归：本地 Python 3.11 **209 项通过**；服务器 Python 3.12.11 的前一轮完整回归为 **207 passed，67.37 秒**，本次新增的 Monitor/Body 环境级演示回归已在本地通过，并在服务器单独复验。新增用例覆盖 GPU 局部映射、端口隔离、服务复用、启动失败/取消和 Worker 异常退出清理、客户端端点绑定及配置校验；同时覆盖 EGL 参数传递。
- 文档中三个完整 Python 示例已实际执行：Module 输出与 patch、Monitor → 安全状态 → Body 中断、TaskQueue 领取/重试/完成。中断示例验证后续 P0 报告不会解除 P3 锁存；不连接真实机器人。
- 当前 17 份 Markdown 的本地链接、86 个 JSON/命令块语法与 XML 片段检查通过；Ruff F 规则、git diff --check 通过。

详细使用见[所有模块说明](../module-guide.zh-CN.md)、[Evaluator/Monitor/TaskQueue](../evaluation-monitoring-and-task-queue.zh-CN.md)、[Worker 模型部署](../worker-model-deployment.zh-CN.md)。新增模型部署回归使用轻量模拟服务，未执行真实六卡模型加载、显存/吞吐压测或真实设备动作。服务器同步不会让已运行服务自动加载新代码，后续新启动或重启才使用改动。

## 2026-09-18 执行与观测改造验证

| 环境 | 结果 |
| --- | --- |
| 本地 Python 3.11 | 完整测试 179 passed |
| 服务器 Python 3.12.11，model-runtime 环境 | 完整测试 179 passed，耗时约 33 秒 |
| 同步位置 | 服务器 `/path/to/deployment`，保留运行数据和已有服务 |

覆盖常驻 Worker、多任务隔离、进程丢失/超时/取消、资源预约、确定性记忆归并、共享观测源、只读查询和已有架构回归。上述测试结果来自本次代码改造验证；中文文档更新已检查 13 份 Markdown 的本地链接、75 个 JSON/命令块语法、CLI 参数与 XML 片段格式；Fake 冒烟返回 completed / success=true / 16 steps。这些检查不计作再次运行完整测试。

未执行真实 GPU/EGL 并行压力测试、实际模型显存测试、真实机器人动作或急停测试。现有模型服务与 Web 未重启。当前没有 Coordinator 崩溃自动恢复，也没有遥测零丢失保证。

使用当前 Python 环境复验：

```bash
python -m pytest -q
```

执行边界见[执行与统一观测](../execution-and-observability.zh-CN.md)，迁移事项见[迁移说明](migration-2026-09.zh-CN.md)。

## 2026-09-16 历史验证

以下保留当日结果，不能作为本次全部检查已重新执行的证明。

本地环境：Python 3.11.10。验证日期：2026-09-16。

| 检查 | 结果 |
| --- | --- |
| 完整 pytest | 130 passed，3 skipped（当前 macOS sandbox 禁止 UDS socket） |
| Python 3.10 Torch 环境导入/compileall | 通过；工具注册、配置加载、model 调用和 Pydantic 1 兼容导入通过 |
| 本机 UDS | 测试代码保留；当前 sandbox 下跳过 socket binding 用例 |
| Ruff F 规则（源码及新增架构测试） | 通过 |
| Mypy 定向检查 execution/scheduler/compiler（follow-imports=skip） | 通过；不是全项目严格类型检查 |
| modules-check / tools-check / monitors-check / graphs-check | 通过 |
| run_fake | completed / success=True / 3 steps |
| examples.service_runtime | completed / success=True |
| wheel 构建 | 成功，无联网安装依赖 |
| 隔离进程从 wheel 导入 | 成功，静态 HTML/CSS/JS 和旧 storage 导入别名均验证 |

新增测试覆盖安全锁存、循环内 fork/join、并行子图身份与位置、checkpoint 续跑/损坏拒绝、失败分支取消、RPC 控制通道与总 deadline、模块启动取消清理、P2 动作准入、导入副作用、NodeResult 写冲突、ServiceContext 边界、双模式 Monitor 的注册/配置/实例复用/安全发布，以及常驻 ToolRuntime 的注册、配置、生命周期、model/memory/skill 调用和 GraphContext 注入。

历史工作区的 CI 配置位于外层 `system/.github/workflows/robotenon.yml`，不在本次独立 RoboTenon 仓库内；该配置配置 Python 3.10–3.12、Pydantic 1/2 与安装/不安装 LangGraph 的组合。本次未触发远端 CI，未在本机实际运行 Python 3.12；Python 3.10 Torch 环境的导入/compileall 与 Pydantic 1 兼容冒烟测试已执行。

未连接机器人、未发布/部署服务、未执行硬件故障验证或性能压测。完整语义和升级注意事项见 migration-2026-09.md。
