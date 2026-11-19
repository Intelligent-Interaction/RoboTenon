# 2026-09-21 Demo 数据下载教程

[English](2026-09-21-demo-data-downloads.md) · [**简体中文**](2026-09-21-demo-data-downloads.zh-CN.md)

增加 RoboCasa demo 的场景资产、模型权重和可选演示轨迹下载步骤，并在 CLI、Portal、仿真教程和文档目录中补上入口。CPU demo 明确标注无需外部数据。

指南优先把大型模型放在仓库之外，使用独立 Hugging Face CLI 环境，提供下载前大小预览与权重校验命令。RoboCasa 资产按上游脚本安装到 RoboCasa 包目录；示范轨迹使用上游数据脚本，说明其用途、默认位置和预览方法。

命令及下载器选项根据 RoboCasa 官方安装/数据说明、上游脚本和 Hugging Face Hub CLI 文档整理。本次只编辑文档，没有下载大型资产或模型，也没有执行 GPU、仿真或设备验证。
