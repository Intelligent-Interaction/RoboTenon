# 统一基准入口与模型迁移（2026-09-29）

[English](unified-benchmark-entry-2026-09-29.md) · [**简体中文**](unified-benchmark-entry-2026-09-29.zh-CN.md)

## 布局与命令

所有基准扩展、benchmark-support 及模型后端现位于
`extensions/benchmarks/` 下。公开的原生评测命令为
`Tenon evaluate`。后端可以是独立文件路径，
也可以是 `module:factory`，无须集中注册。旧扩展命令转发到共享参数解析器，
调用者须提供 `--backend`。原 VLA 工厂仍支持已有的
生成配置。

后端 JSON 会将模型参数原样传给工厂。
协议配置由各基准维护。设备数、Worker 数、组批上限和调度均属执行选项。
模型可使用任意支持的基准配置，包括冒烟或子集模式。
期望数量取自实际枚举的 episode，未硬编码 Xiaomi 的分母。
报告区分内置、外部和覆盖配置，
并记录完整的生效配置和后端声明。

## 调度

默认使用独立的随机数生成器洗牌（种子 7），随后进入核心的
全局待执行队列与空闲进程槽队列。洗牌保留每个案例的原始身份、原生任务索引、
episode 索引和环境参数。
Worker 槽固定绑定 GPU、模型端点和 EGL，启动时交错分布到各张 GPU。
核心 attempt 记录包含 Worker/GPU 身份及时间信息。
`episode-plan.json` 记录初始队列顺序，`protocol.json` 记录调度策略。
重试复用同一 episode 身份，最终统计只计算一次结果。

可选的 `--scheduling-strategy static` 将 `case.index % worker_count` 分配到
固定槽位，允许负载不均。`--no-shuffle` 可独立于
调度策略使用。核心代码仅将三处字面的
`backend == "process"` 配置限制替换为已有的
`managed_workers` 能力检查，使扩展的静态进程后端
复用既有资源、重试及 Worker 生命周期逻辑。

## 验证证据

本地完整回归为 **903 项通过、2 项跳过**（本机缺少 torch，另一项
是 Linux 专用的 Worker 故障检查）。服务器模型协议套件的 **16** 项检查均通过，
包括整模型 BF16 缓冲区回归。

- 外部文件后端支持相对导入，各自使用独立的模块命名空间。
- 子进程测试确认模拟器不导入模型专用模块。
- batch 1 和 batch 2 使用相同 Graph，覆盖逆序回复路由、
  非 Xiaomi 的历史与重规划声明、立即终止及会话清理。
- 测试主动终止 Worker 后成功重试，最终得到六条唯一的 episode 记录，
  未增加错误计数或重复计数。
- 独立洗牌保留所有案例数据及 Python 全局 RNG 状态。
- 真实服务器模型在 batch 1 与 8 使用相同观测和噪声对照，
  迁移前后动作最大绝对差均为 **0.0**。见
  [原始一致性结果](evidence/unified-benchmark/model-migration-parity.json)。
- 模拟策略与真实 Xiaomi 各完成 **4 个** RoboCasa365 冒烟 episode（两个任务，
  每任务两个 episode，上限 24 步），**错误与取消均为零**。Worker 的 CUDA/EGL
  绑定 GPU 0 和 1。模型使用 `model-runtime`（Python 3.12），
  模拟器使用独立的 RoboCasa 环境（Python 3.11），没有注入模型环境的
  site-packages。服务关闭后 GPU 显存占用归零。
- 模拟策略确实形成大小为 2 的批次。真实 Xiaomi 的短测请求
  未在 50 ms 内合批，所有前向批大小均为 1。上文真实模型的
  batch-8 输入一致性验证覆盖了迁移后的批处理实现。

24 步冒烟没有任务成功，用于验证运行链路。此前修复后的
CloseFridge 47/50 结果早于此次结构迁移。
本记录没有新增 target50 × 50 全量准确率结果。其他模拟器协议
由既有契约回归覆盖，本轮没有重新执行其完整物理仿真
或 Isaac 评测。EGL 析构保护和显式整模型 BF16
转换均保留。

## 服务器产物

`/path/to/deployment/validation-xiaomi-20260929/` 下包含以下内容。

- `model-migration-parity.json` 与 `.log`
- `unified-migration/fake/` 与 `unified-migration/xiaomi/`
- `model-env-install.log` 记录在服务器 model-runtime 中直接安装 Transformers
  4.57.1、替换不兼容版本的过程。
