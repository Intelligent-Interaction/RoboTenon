# 真机接入

[English](real-robot.md) · [**简体中文**](real-robot.zh-CN.md)

RoboTenon 核心提供 `RealRobotAdapter`、`RealRobotBody`、安全状态和观测缓存，
但仓库不再提供绑定旧 XR365 planner/verifier 的真机主配置。站点需要独立
extension 来实现设备驱动、相机源、动作格式和安全策略。

一个站点 extension 应注册：

- Environment：从机器人和相机读取 `EnvironmentFeedback`；
- Body：把 `ActionChunk` 交给厂商驱动，并实现 hold/stop；
- TaskAdapter：枚举站点任务和 trial；
- VLA 或 Tool：生成与 `EnvironmentIO.action` 一致的动作；
- Graph profile：明确 reset、推理、执行、观测和终止流程。

先在 dry-run 或硬件仿真模式验证 IO schema、动作维度、超时、急停和断连恢复，
再接真实设备。benchmark extension 的生成配置面向仿真评测，不应直接当成
真机控制配置。
