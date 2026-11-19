# 假规划器与验证器

[English](README.md) · [**简体中文**](README.zh-CN.md)

仅用于系统 Graph 的请求路由和生命周期测试，不是学习模型，不代表规划或验证能力。

在模型环境安装：

```bash
python -m pip install -e extensions/benchmarks/models/fake_reasoner
```

框架服务工厂：`robotenon_fake_reasoner.backend:Provider`。
参数 `role` 为 `planner` 或 `verifier`。planner 返回原任务作为 subgoal；verifier 始终返回 subtask_success=false。返回顺序刻意反转，以验证 request_id 路由。

本包不需要下载资产或权重，不依赖仿真库。运行示例见 [RoboCasa 分层系统](../../systems/robocasa_hierarchical/README.zh-CN.md)。
