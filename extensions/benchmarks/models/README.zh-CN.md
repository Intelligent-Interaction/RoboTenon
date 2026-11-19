# 模型后端接口

[English](README.md) · [**简体中文**](README.zh-CN.md)

[返回首页](../README.zh-CN.md) · [首次运行](../QUICKSTART.zh-CN.md) · [并行评测](../EVALUATION.zh-CN.md)

本页面向模型实现者。只运行已有模型时，先按快速开始完成仿真安装和 smoke，再填写该模型的配置。以下接口对所有原生 benchmark 通用；支持范围与 action 转换仍由模型后端负责。

编写独立的 `vla.py`，并将其路径或 `module:factory` 传给
`Tenon evaluate --backend ...`。文件后端的工厂为
`create()`。支持 `from .preprocessing import transform` 等相对导入。
即使不同目录中的文件都叫 vla.py，也无需修改注册表。

```python
SPEC = {
    "api_version": 1,
    "supported_benchmarks": ["robocasa365"],
    "supports_batch": True,
    "history": {"length": 4, "interval": 2},
    "replan_steps": 16,
}

def create(**params):
    return VLA(**params)
```

工厂返回的对象需实现以下方法。

- `open_session(session_id, task_info)`。元数据包含 `benchmark_id`、稳定的
  `episode_id`、`task`、`action_spec` 以及 `observation_spec`。
- `act_batch(requests)`。每个请求包含 `request_id`、`session_id`、`step_id`
  和 `observation`。观测中包含 `history`（按 SPEC 采样、首帧补齐，并按由旧到新排列）
  以及任务说明 `task_description`。
- `close_session(session_id, result)` 释放该 episode 的缓存。
- `close()` 释放模型资源。

每个请求返回一个结果，通过原始的 `request_id` 对应。

```python
{"request_id": request["request_id"],
 "action": {"native_actions": [{"actions": action_vector}, ...]}}
```

原生动作字典随 benchmark 而异，内容不一定是向量。
运行时最多执行 `replan_steps` 步，每一步都采集观测，
一旦成功或终止便丢弃剩余动作。每个模型副本只加载一次权重。
缓存和 RNG 状态必须按 session 隔离。若需避免调度影响 RNG，
请根据稳定的 episode 身份设种，不要使用 Worker 编号或接收顺序。

`backend-config` 是原样传给 `create(**params)` 的 JSON 对象。
`SPEC` 为静态声明，Worker 启动前会进行校验。仅支持单样本时设为 `supports_batch=False`，
此时 batch size 大于一会报错。提供 batch 接口本身
无法证明模型真正执行了合批计算。

在两个环境中都安装 RoboTenon 和共享运行时，模型依赖只装在
`--model-python`，benchmark 与仿真器依赖装在 `--sim-python`。
无需跨环境注入 site-packages。启动器会在模型解释器中重新执行，
启动常驻的模型服务，再通过独立解释器运行仿真。
路径应指向同一台主机的文件系统。

[`xiaomi/`](xiaomi/README.zh-CN.md) 保留已验证的 Xiaomi 预处理、BF16 转换及两种 RNG
模式。系统 Tool 的兼容入口转发至这一独立实现。`fake/` 只用于协议测试，其 `action`
需配置为所选 benchmark 支持的原生动作字典。

实时 profile 使用同一 batch 后端 API。推理可能经过多个环境 tick
才完成。观测按 FIFO 入队，返回的动作 chunk 写入消息流，
下一 tick 使用最近完成的动作。
没有新结果时沿用 benchmark 的保持动作。`close_session` 会收到 `rt_metrics`。
模型专用的持续推理或延迟感知策略需要显式实现；
不能从 `supports_batch` 自动推断。

## HLP / verifier 等非动作模型

完整系统沿用 Module / Tool / Graph。VLA 保持本页接口；返回 plan/verdict 的模型实现框架通用 Provider，不把它们伪装成 native action。请看 [系统接入手册](../SYSTEMS.zh-CN.md) 和 [fake reasoner 示例](fake_reasoner/README.zh-CN.md)。

## 分层系统的真实规划/验证模型

[Qwen3-VL reasoner](../systems/robocasa_hierarchical/tool/qwen/README.zh-CN.md) 提供真实的常驻 batch Provider，供 HLP / verifier 共用；它返回结构化决策，不输出机器人动作，因此使用通用 BatchProvider 接口。Xiaomi 仍通过独立 `vla.py` 输出动作。两者可由 [分层系统 Graph](../systems/robocasa_hierarchical/README.zh-CN.md#托管真模型版本推荐) 一次启动。

## 已有模型

- [Xiaomi Robotics-1](xiaomi/README.zh-CN.md)：RoboCasa365，支持动态 batch 和独立模型环境。
- [OpenVLA-OFT](openvla-oft/README.zh-CN.md)：原版 LIBERO-Spatial，batch=1；已完成500个episode，成功率98.20%，验证范围见模型说明。
