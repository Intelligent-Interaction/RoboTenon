# 文档图片

[English](README.md) · [**简体中文**](README.zh-CN.md)

本目录保存当前教程和参考手册使用的图片。技术图采用 SVG，可直接编辑文字与连线；Portal 截图采用 PNG，保留实际页面内容。

## 技术示意图

| 图片 | 用途 |
| --- | --- |
| [运行路径](diagrams/run-paths.zh-CN.svg) | 安装、CLI、Portal、结果与部署入口 |
| [组件架构](diagrams/architecture.zh-CN.svg) | Graph、Environment、Body、Tool 与安全状态 |
| [配置合并](diagrams/config-layers.zh-CN.svg) | 主配置、覆盖层、有效配置和装配 |
| [仿真服务](diagrams/simulation-services.zh-CN.svg) | RoboCasa、Qwen 与 Xiaomi 的进程关系 |
| [Demo 资源下载](diagrams/download-resources.zh-CN.svg) | CPU 示例、仿真文件与可选演示轨迹的区别 |
| [结果文件](diagrams/result-files.zh-CN.svg) | 摘要、episode、执行尝试与数据源 |
| [实验生命周期](diagrams/experiment-lifecycle.zh-CN.svg) | 提交、排队、执行与资源清理 |
| [轨迹页导航](diagrams/portal-navigation.zh-CN.svg) | 任务、媒体、节点与调用耗时的阅读顺序 |
| [Graph 数据](diagrams/graph-data.zh-CN.svg) | 输入绑定、节点输出与 Graph 对外输出 |
| [节点工作区](diagrams/node-context.zh-CN.svg) | 私有执行、提交与公开结果 |
| [观测缓存](diagrams/observation-cache.zh-CN.svg) | 帧数据、缓存与查询消费者 |
| [插件分工](diagrams/plugin-map.zh-CN.svg) | 业务、设备与基础设施扩展位置 |
| [任务与安全](diagrams/safety-queue.zh-CN.svg) | Evaluator 任务判断和 Monitor 安全停止 |
| [Worker GPU](diagrams/worker-gpu.zh-CN.svg) | 每 Worker 三张卡的编号映射示例 |
| [真机部署](diagrams/real-robot.zh-CN.svg) | 模型、控制主机、观测、驱动与停止路径 |

图中连线用于说明职责和数据关系，不表示性能测量或真实设备的物理布线。详细约束以对应正文为准。

## Portal 截图

以下截图于 2026-09-21 从本地隔离的 Portal 采集，使用 [CPU 示例](../tutorials/examples/cpu-demo.json)实际运行三个任务。未使用生产实验数据，也不代表仿真或真机画面。

| 图片 | 内容 |
| --- | --- |
| [创建实验](tutorials/portal-create.zh-CN.png) | 配置选择、保存 Trace 和启动检查 |
| [实验列表](tutorials/portal-experiments.zh-CN.png) | 一次完成 3 / 3 任务的 CPU 实验 |
| [节点状态](tutorials/portal-nodes.zh-CN.png) | 轨迹页运行状态面板展开为全屏后的节点与输入输出 |
| [Trace 查询](tutorials/portal-traces.zh-CN.png) | 对本次实验执行只读 SQL 的结果 |

更新界面时重新采集截图，不修改截图中的状态、数字或运行结果。新增技术图时保留 SVG 的标题、说明和正文中的替代文字，便于检索和阅读。

## 项目标志

[SVG 矢量标志](../../robotenon/web/static/tenon.svg) · [512 像素 PNG 头像](robotenon.png)
