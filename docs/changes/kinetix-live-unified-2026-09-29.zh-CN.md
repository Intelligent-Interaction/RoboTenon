# 通过统一评测器运行 Kinetix 实时评测

[English](kinetix-live-unified-2026-09-29.md) · [**简体中文**](kinetix-live-unified-2026-09-29.zh-CN.md)

## 实现

`robotenon_benchmark_support.evaluate --benchmark kinetix --profile realtime`
与同步配置共用后端加载器、模型进程、组批、episode 队列以及结果
汇总机制。实时配置生成的 Graph 在观测节点和终止判断之间显式插入
`live_tick`。物理推进仍由 Body 负责，
后台任务不推进模拟器。

实时 Tool 按 FIFO 将每次观测加入队列，异步处理模型回复。
历史帧需求来自后端 SPEC。动作块作为模型消息消费，
已缓存的动作不重复执行前向推理。每个模拟器 tick 优先使用
最新收到的动作，若无则调用 `benchmark.get_hold_action(last_fresh_action)`。
Kinetix 初始保持全零指令，此后重复最近一次新指令。默认首个 tick
不等待推理。设置 `wait_first_action=true` 后最多等待
30 秒。原生终止信号立即停止动作和新观测，
仅为正确关闭会话而等待已发出的推理请求结束。

时钟遵循固定版本评测框架中的 LiveEpisodeRunner、ActionBuffer 和
Clock。计时排除准备时间，每个 tick 等待本周期剩余时间除以 pace 的时长。
慢步之后不密集补跑 tick，原生 done 后不再按周期等待。
仅因评测器 horizon 结束时，仍按上游协议发送最后一条非原生终止观测，并完成周期等待。
实时计数器与实际经过时间记录在
`outputs.rt_metrics`。常规 seed、horizon 与成功统计保持原义。
模型专用的持续推理及延迟感知动作选择需要显式配置，
默认不会开启。

## 验证

- 添加最后一个异常传播检查之前，仓库全量回归为 **946 项通过、2 项跳过**。
  跳过原因为本机缺少 torch 和一项 Linux 专用测试。
  最终的实时、入口与后端专项套件 **53 项通过**。
- 包含实时配置在内，20 个基准入口的 **157 个配置**均可生成和加载。
- 慢模型集成测试运行真实模型服务进程与模拟器进程，
  覆盖 150 ms 推理、50 Hz 环境 tick、两种首个动作模式、两个会话、
  保持动作、第六步终止、指标和正常关闭。
- 即使后台推理异常与终止消息同时到达，
  评测也会保留异常，不会误计为成功。
- 真实 Kinetix CPU 冒烟完成 **2 个 episode × 20 步，运行时错误为零**，实际形成两个
  大小为 2 的模型批次。使用符号观测、Cartpole Thrust、seed 0、动作
  噪声 0.1、目标 10 Hz、pace 1。见[原始报告](evidence/kinetix-live/smoke.json)。
- CPU JAX 编译使实际控制频率约为 **2.1 Hz**。
  报告记录的是实测频率，未达到目标值。
- 两个模拟策略 episode 均未完成任务。本次冒烟验证运行时行为，
  没有测量模型准确率或完整 RTC 基准结果。

本轮没有替换服务器代码，也没有重启正在运行的 8 GPU RoboCasa365 全量评测或借用其进程。
真实冒烟使用了现有的本地 CPU
Kinetix 环境及已有的 RTC 资产。

## 其他基准审计修复

LIBERO-Plus、MolmoSpaces、RoboMME 和 Simpler 的兼容命令现保留目录声明的默认配置。
在加载模型前拒绝无效的协议值。
底层 `configure` CLI 会引导实时模式用户使用统一评测器，
由其提供所需的后端 SPEC 和异步 Tool。
