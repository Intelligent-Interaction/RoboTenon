# RoboTenon 文档

[English](README.md) · [**简体中文**](README.zh-CN.md)

如果你第一次接触 RoboTenon，从[快速上手](quickstart.zh-CN.md)开始。它会先带你跑通一个不需要 GPU 的例子，再解释刚才发生了什么。

这套文档分成三类：

- **教程**：跟着做，目标是完成一件事。
- **参考手册**：用到时查，不需要从头读。
- **更新记录**：升级或排查历史问题时看。

## 新手学习路线

这条路线按照“先跑通，再理解，再换成真实环境”的顺序组织：

1. [快速上手](quickstart.zh-CN.md)：5 分钟确认安装可用。
2. [首次运行](tutorials/01-first-run.zh-CN.md)：看懂最小配置和一次 Fake 任务。
3. 选择一个入口：[Python API](tutorials/python-api.zh-CN.md)、[命令行](tutorials/cli-experiments.zh-CN.md)或 [Web Portal](tutorials/03-portal.zh-CN.md)。
4. [查看结果](tutorials/view-results.zh-CN.md)：学会判断“流程跑完”和“任务成功”的区别。
5. [使用 extensions](tutorials/extensions.zh-CN.md)：接入仿真器、模型或设备。
6. 进入真实场景：[RoboCasa 仿真](tutorials/02-first-simulation.zh-CN.md)或[真机部署](tutorials/real-robot.zh-CN.md)。

不熟悉文档里的名词时，随时查[术语表](glossary.zh-CN.md)。

## 按目标找教程

| 你现在要做什么 | 教程 | 跑完后会得到什么 |
| --- | --- | --- |
| 确认框架能不能用 | [首次运行](tutorials/01-first-run.zh-CN.md) | Fake 冒烟结果和配置检查结果 |
| 嵌入自己的 Python 程序 | [Python API](tutorials/python-api.zh-CN.md) | 单个 episode 和一批 experiment |
| 在终端跑正式实验 | [命令行实验](tutorials/cli-experiments.zh-CN.md) | 3 个 CPU 任务、结果文件和 Trace |
| 在网页运行和查看 | [Web Portal](tutorials/03-portal.zh-CN.md) | 实验列表、任务进度、节点和轨迹 |
| 评测 VLA 与配置并行仿真 | [Benchmark 使用指南](benchmarks.zh-CN.md) | 独立 `vla.py`、统一 CLI、动态 batch 和成功率统计 |
| 安装可选环境或模型 | [使用 extensions](tutorials/extensions.zh-CN.md) | 可被配置加载的 provider |
| 判断任务是否真的成功 | [查看结果](tutorials/view-results.zh-CN.md) | 文件、页面、Trace 和视频的对照方法 |
| 跑 RoboCasa | [首次仿真](tutorials/02-first-simulation.zh-CN.md) | 单个仿真任务和一个小批量 |
| 下载场景与模型 | [数据与模型下载](tutorials/download-data.zh-CN.md) | 可核对的资产目录和权重文件 |
| 控制真实机器人 | [真机部署](tutorials/real-robot.zh-CN.md) | 驱动接入、观测接入与上线检查 |
| 写一个新流程步骤 | [自定义模块](tutorials/04-custom-module.zh-CN.md) | 一个可运行的双节点 Graph |
| 配置 RoboCasa365 多卡评测 | [Xiaomi + RoboCasa365](tutorials/benchmark-h100-xiaomi.zh-CN.md) | 8 卡、224 个 Worker、启动命令与完整结果 |
| 配置 VLABench 多卡评测 | [Xiaomi + VLABench](tutorials/benchmark-h100-xiaomi-vlabench.zh-CN.md) | 8 卡、160 个 Worker、启动命令与完整结果 |
| 配置 LIBERO 多卡评测 | [Xiaomi Robotics 0 + LIBERO](tutorials/benchmark-h100-xiaomi-libero.zh-CN.md) | 四 suite、2,000 个 episode、复现与排错 |
| 配置 LIBERO-Plus 多卡评测 | [Xiaomi Robotics 0 + LIBERO-Plus](tutorials/benchmark-h100-xiaomi-libero-plus.zh-CN.md) | 8 卡、288 个 Worker、10,030 个案例与完整结果 |
| 部署到多卡服务器 | [8 卡服务器部署](tutorials/server-8gpu.zh-CN.md) | Worker、GPU 和模型服务的部署方案 |

完整教程索引见[教程目录](tutorials/README.zh-CN.md)。

## 参考手册

这些页面适合查具体问题，不建议新手连续通读。

### 运行与部署

| 文档 | 什么时候查 |
| --- | --- |
| [Benchmark 使用指南](benchmarks.zh-CN.md) | 从资产准备到完整评测、结果与吞吐查询 |
| [Benchmark 参数手册](../extensions/benchmarks/EVALUATION.zh-CN.md) | 查多 GPU、调度、batch、回收、超时和运行限制 |
| [执行与观测](execution-and-observability.zh-CN.md) | 查运行参数、执行后端、结果目录、停止与恢复 |
| [Worker 模型部署](worker-model-deployment.zh-CN.md) | 查 GPU 编号、模型端口、进程和生命周期 |
| [观测缓存](observation-cache.zh-CN.md) | 查图像帧引用、缓存、持久化和跨进程使用 |
| [Benchmark 迁移与验证](benchmark-migration.zh-CN.md) | 查 20 个 benchmark 的配置入口、真实仿真覆盖和待验证项 |
| [上游依赖](vendor-guide.zh-CN.md) | 查 RoboCasa、模型服务和第三方资料 |
| [常见问题](troubleshooting.zh-CN.md) | 已经遇到报错或结果不符合预期 |

### 架构与开发

| 文档 | 什么时候查 |
| --- | --- |
| [Benchmark Graph 与模型契约](native-benchmark-graph.zh-CN.md) | 查生成的 Graph、独立模型接口与生命周期 |
| [架构说明](architecture.zh-CN.md) | 想知道组件职责、调用链和资源由谁管理 |
| [模块参考](module-guide.zh-CN.md) | 查各子系统的输入、输出和代码入口 |
| [开发手册](development-guide.zh-CN.md) | 开发新组件或组合复杂配置 |
| [Graph 工作流](workflow.zh-CN.md) | 写条件、并行、循环和恢复流程 |
| [普通 Graph 动态 batching](graph-batching.zh-CN.md) | 不依赖 benchmark 启动共享模型，配置多卡、合批并检查结果 |
| [显式 Graph 数据接口](explicit-graph-data.zh-CN.md) | 配置节点输入、输出和跨节点引用 |
| [节点私有上下文](node-private-context.zh-CN.md) | 处理读写隔离、结果发布和 checkpoint |
| [业务插件](plugin_development.zh-CN.md) | 开发 Module、Tool、Body、Environment 等 provider |
| [基础设施插件](infrastructure-plugins.zh-CN.md) | 开发执行后端、存储、RPC 或观测模态 |
| [评估、监控与任务队列](evaluation-monitoring-and-task-queue.zh-CN.md) | 接入任务判断、安全中断或队列 |

[架构汇报稿](../robotenon-design-report.zh-CN.xml)用于方案沟通；实际配置字段和命令以代码及参考手册为准。

## 更新与历史记录

[更新记录目录](changes/README.zh-CN.md)保存版本迁移、修复记录、性能测量、故障调查和审计附件。记录里的测试数字只代表当时的代码和环境，不等于当前工作区的测试结果。

## 文档维护约定

- 教程开头先说“完成后能得到什么”，再写前提和步骤。
- 每个关键步骤都给出命令、预期结果和失败后的入口。
- 第一次出现专业名词时用一句人话解释，并链接到[术语表](glossary.zh-CN.md)。
- 可直接运行的例子和只用于说明接口的片段要明确区分。
- 所有示例路径相对仓库根目录；外部资源统一使用 `/opt/envs/...`、`/data/models/...` 或 `/path/to/...` 等明确占位符。
- 面向使用者说明功能、参数含义和选取方式；不写当前运行进度、临时部署过程或聊天交接措辞。
- 不公开个人目录、真实主机/账号、私有环境名、访问凭据或机器专用配置；历史变更记录也须脱敏。
- 性能数字若保留，必须标注测试条件与适用边界；不能把实验快照当作产品能力或正式准确率。
- 配置或 CLI 改动同步更新对应参考页，不在首页堆版本说明。
- 变更、事故和验证证据放进 `changes/`，不要混进新手教程。
- 第三方 README 与许可证保留原文；RoboTenon 的接入说明写在本项目文档中。
