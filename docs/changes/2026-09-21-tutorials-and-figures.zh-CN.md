# 2026-09-21 教程与配图

[English](2026-09-21-tutorials-and-figures.md) · [**简体中文**](2026-09-21-tutorials-and-figures.zh-CN.md)

本次更新补齐命令行、Web Portal、结果查看和真机部署教程，并为当前使用文档增加技术图和操作截图。

## 内容

- 新增[命令行运行实验](../tutorials/cli-experiments.zh-CN.md)，覆盖预检、批量、单次执行、结果读取和只读查询 API。
- 重写 [Portal 教程](../tutorials/03-portal.zh-CN.md)，从无 GPU 的 CPU 实验开始，再说明切换到 RoboCasa 的步骤。
- 新增[查看实验结果](../tutorials/view-results.zh-CN.md)，对照文件和页面解释状态、成功信号、执行尝试、Trace、指标、图像和视频。
- 新增[真机部署](../tutorials/real-robot.zh-CN.md)，说明站点驱动、观测源、插件装配、动作契约、停止验收和两种启动方式。
- 提供一份[正式 CPU 实验配置](../tutorials/examples/cpu-demo.json)，供 CLI 和 Portal 共用。
- 增加 15 张 SVG 技术图与 4 张真实 Portal 截图，统一保存在[图片目录](../assets/README.zh-CN.md)。
- 更新首页、教程目录及相关参考页的入口和图片；历史审计附件继续保存在本目录。

## 验证

在 Python 3.11、macOS 的独立临时目录中验证，无 GPU、模型或机器人连接：

| 检查 | 结果 |
| --- | --- |
| CLI 教程中的执行与读取命令 | 10 个命令块通过；查询接口使用临时端口 |
| CPU 批量实验 | 3 个任务完成，3 个成功，0 个错误 |
| CPU 单次实验 | Graph 完成，任务成功，实际结果和数据源可读取 |
| Portal 创建、预检和提交 | 3 / 3 任务成功 |
| Portal 任务与节点查看 | 三个节点完成，输入与输出可查看 |
| 结果教程的 SQL | 3 个查询均返回预期记录 |
| 只读 HTTP 查询 | GET Trace 与 POST SQL 均返回记录 |
| 配图 | 15 张 SVG 渲染检查，4 张截图核对实际页面 |

Markdown 本地链接与锚点、活动文档中的 JSON / Shell 代码块，以及文档差异的空白检查均通过。核验结束后关闭了临时浏览器标签和演示服务。

## 按实际行为修正的说明

进程后端的单次运行以 `--output` 作为输出基准，实际结果在执行尝试目录中，使用返回的 `output_path` 定位。

只读服务提供 Trace / SQL / Metrics API；当前首页仍是完整实验列表，禁用实验管理时不能按完整 Portal 页面操作。CLI 结果也不会自动注册到 Portal 实验列表。

CPU 示例没有传感器图像；轨迹中的无效媒体项不作为相机结果。截图中的成功次数只说明环境就绪检查通过。

真机章节是接入与验收指南，没有宣称模板可直接控制任意设备。本轮没有进行 GPU 推理、仿真渲染或物理机器人验收。
