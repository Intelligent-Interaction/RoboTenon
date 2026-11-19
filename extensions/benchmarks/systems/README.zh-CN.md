# 被评测系统

[English](README.md) · [**简体中文**](README.zh-CN.md)

系统使用框架原生 **Module / Tool / Graph** 表达决策流程。模型权重与推理代码放在 [`../models/`](../models/README.zh-CN.md)，环境和评测协议留在各 benchmark extension。

- [完整系统接入手册](../SYSTEMS.zh-CN.md)：目录、配置、动态 batch、模型服务、环境隔离和结果。
- [RoboCasa 分层系统](robocasa_hierarchical/README.zh-CN.md)：迁移指定提交中的规划/VLA/验证 Graph，使用当前 RoboCasa365 Environment、Body、TaskAdapter。

新系统推荐建立独立可安装包，包含 `configs/`、`src/<package>/modules.py` 和 README。无需修改框架注册表；通过 plugin_modules / plugin_tools 加载，也可以通过绝对路径引用仓库外的配置。

## 已提供的运行方式

| 系统 | 启动方式 | 验证范围 |
|---|---|---|
| Xiaomi + Qwen3-VL 分层系统 | `robocasa_hierarchical/configs/system.qwen_batch.json`；通过统一 evaluate 加载 Graph | 真实单样本、分卡及同卡动态 batch 短程验证通过；正式成功率另行评测 |
| fake 分层系统 | `system.json` + `execution.fake.json` | 真实进程和 Graph 测试通过，生成是 fake |
| Xiaomi + 外部 Qwen API | `system.qwen.json` | 可配置的旧 HTTP 接入，需要自行启动 API |

纯 Xiaomi VLA 使用统一 evaluate 的默认 rollout Graph，不属于这里的 HLP/verifier 分层系统。完整安装、smoke 与 full 命令见上述分层系统 README。
