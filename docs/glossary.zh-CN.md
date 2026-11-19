# RoboTenon 术语表

[English](glossary.md) · [**简体中文**](glossary.zh-CN.md)

这不是严格的 API 定义，而是一份“先把文档看懂”的速查表。精确字段和接口以对应参考手册为准。

## 名称与入口

| 名称 | 人话解释 |
| --- | --- |
| RoboTenon | 项目和框架的名称 |
| `robotenon` | Python 包名，命令行入口为 `Tenon` |
| CLI | 在终端里执行的命令，例如 `Tenon run-fake ...` |
| Python API | 在自己的 Python 代码里 import 并调用 RoboTenon |
| Web Portal | 用浏览器提交、查看和控制实验的页面 |

## 一次运行里的对象

| 名称 | 人话解释 | 例子 |
| --- | --- | --- |
| Task | 要完成的目标 | 把杯子放进柜子 |
| Episode | 对一个任务的一次尝试 | 用种子 7 尝试一次 |
| Experiment | 一批 episode 的集合 | 10 个任务各跑 3 次 |
| Run | 一次实际启动；通常有自己的 `run_id` 和目录 | `cpu-demo-02` |
| Attempt | 某个 episode 的一次执行尝试；失败重试会产生新的 attempt | 第一次进程崩溃，第二次重试成功 |
| Seed | 随机种子，用来尽量复现同一组随机条件 | `7`、`8`、`9` |

## 运行组件

| 名称 | 人话解释 |
| --- | --- |
| Config | 运行清单，决定加载哪些组件、参数和流程 |
| TaskAdapter | 任务清单生成器，负责告诉框架“这批实验要跑哪些任务、各跑几次” |
| Worker | 干活的执行进程，持有环境、模型等运行资源 |
| Environment | 机器人所在的世界；可以是 Fake、仿真器或真实现场 |
| Session | 框架与某个 Environment 的一次连接和交互周期 |
| Body | 动作出口，把“移动到这里”之类的动作真正发给仿真器或机器人 |
| Graph | 流程图，规定步骤的先后顺序、分支、循环和结束条件 |
| Node | Graph 里的一个步骤 |
| Module | 可复用的业务步骤，例如感知、规划、决策或评估 |
| Tool | 被 Module 调用的能力，例如模型、记忆或技能库 |
| Monitor | 旁路观察运行状态的组件，可用于告警或触发安全停止 |
| Evaluator | 判断任务结果的组件，例如物体是否真的放进目标区域 |
| Extension | 单独安装的接入包，用来增加仿真器、模型、设备或任务集 |
| Provider | Extension 向框架登记的一类实现，例如一个 Environment 或 Tool |

## 结果与排查

| 名称 | 人话解释 |
| --- | --- |
| Output / Result | 本次任务最终返回的结构化结果 |
| Trace | 运行过程的时间线，记录每个步骤何时开始、结束、成功或失败 |
| Metric | 可统计的数字，例如成功率、耗时或吞吐量 |
| Observation | 环境给机器人的信息，例如图像、关节状态或任务状态 |
| Feedback | 环境对动作或状态的回报，例如动作是否完成、环境是否健康 |
| Checkpoint | 为恢复运行保存的中间状态 |
| Artifact | 运行产生的文件，例如结果 JSON、视频、日志或配置快照 |

## 最容易混淆的状态

`completed` 和 `success` 不是一回事：

- `workflow_status: completed` 或 `graph_status: completed`：流程走完了，没有卡在中途。
- `success: true` 或 `task_success: true`：任务目标达成了。

例如机器人顺利执行完整套流程，但最后没抓住杯子：流程是 `completed`，任务仍然是 `false`。排查时先看流程状态，再看任务结果和 `reason`。

## 三种环境不是三套框架

- **Fake**：只验证框架流程，不模拟真实物理，也不控制设备。
- **仿真**：在 RoboCasa、LIBERO、RoboDojo 等模拟世界中执行。
- **真机**：通过设备驱动读取传感器并发送动作。

三者共用配置、Graph、运行入口和结果格式。通常先用 Fake 验证流程，再用仿真验证任务，最后做真机验收。
