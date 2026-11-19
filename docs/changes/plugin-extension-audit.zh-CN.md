# 插件接入边界检查与修复（2026-09-20）

[English](plugin-extension-audit.md) · [**简体中文**](plugin-extension-audit.zh-CN.md)

目标是在既有公共协议下新增环境、数据集、模型、驱动、执行后端、持久化与流传输，
只添加插件及配置，不在 Runner/实验入口按供应方名称添加分支。详细接口见[基础设施插件](../infrastructure-plugins.zh-CN.md)。

| 检查项 | 当前状态 |
| --- | --- |
| 六类业务 provider | 已支持外部包 plugin_* 导入，不需要修改 load_builtin_* |
| 环境参数 | 配置 → TaskCase → EpisodeRequest 递归合并进入 SessionSpec；规范字段保持优先 |
| 批量与 CLI | 按实例 ID/注册名覆盖 TaskAdapter；split 可自定义；快捷字段由 provider 的 cli_overrides 声明 |
| 执行后端 | 注册工厂与能力元数据；单任务/批量共用 execute/close；启动器不直接导入进程实现 |
| 四类持久化 | safety/context/actions/observation 独立注册，配置指定 plugin_stores/backend/params |
| RPC 流传输 | JSON-lines 服务协议与连接/监听实现分离，Body 执行与控制连接均使用同一传输配置 |
| 观测模态 | Frame.modality/producer 和 Channel.modality 开放非空字符串；明确模态不再被猜成 other |
| 资源生命周期 | 统一应用资源容器处理启动失败与正常关闭；自定义 store 关闭支持同步/异步 |
| 动作和模型接口 | 12 维约束保留在 RoboCasa Body/Xiaomi Tool；新插件声明自己的 IO 与动作格式 |
| 结果判定 | Graph outputs 显式映射，原生成功/超时由环境和 Body 转换 |

验证覆盖：外部环境/TaskAdapter 的单进程与 spawn 多进程连续任务；外部执行后端的单任务与批量入口；
四类存储的装配、路径与关闭；失败启动清理；动作存储关闭重启；外部流传输的 Environment 与 Body RPC；
自定义传感器从 Frame 到缓存/resolver 的数据往返；以及禁止入口按后端名称分支的架构测试。
最新完整回归数量见[验证记录](verification-2026-09.zh-CN.md)。

保留的统一契约与部署边界：

- Graph 调度语义、安全等级、JSON 序列化、进程内单环境与 Body 动作边界保持统一；插件不能改变这些契约后仍宣称兼容。
- provider 的 shared 等能力是实现承诺，注册表不替插件实现分布式一致性、认证、调度、硬件保护或驱动。
- 内置 Web/Trace 查询仍使用显式 SQLite/JSONL 数据源。新存储若需现有 Portal 展示，应提供兼容投影或查询适配。
- 内置观测 memory/filesystem 是历史 hybrid 别名；自定义缓存可另行实现，remote 必须提供真实远程端点。
- Python/CUDA/MuJoCo 冲突仍通过独立部署解决；本次未实现 LIBERO 插件或运行真实 GPU/机器人实验。
