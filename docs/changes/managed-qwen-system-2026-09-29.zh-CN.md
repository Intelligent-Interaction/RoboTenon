# Xiaomi + Qwen 分层系统托管接入

[English](managed-qwen-system-2026-09-29.md) · [**简体中文**](managed-qwen-system-2026-09-29.zh-CN.md)

## 修复的缺口

此前 systems 中只有 fake 服务配置和依赖用户另开 Qwen API 的模板；正在运行的纯 Xiaomi 默认 Graph 也不是分层系统。现在新增真实 Qwen3-VL Provider、托管 batch 服务配置和系统 Module/Tool/Graph 配置，可由统一 evaluate 启停所有模型服务。

- Qwen 模型代码位于 `extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen`，不嵌入 benchmark。
- planner 和 verifier 两个 Tool 显式传递 purpose，共用一个或多个 `reasoner` 模型副本；请求仍按 request_id/session 路由。
- 当前 native 图像直接送入 Qwen processor，JSON schema 校验和截断重试都在模型进程中完成。
- 系统部署配置支持同卡部署 Xiaomi/Qwen，也支持分卡部署；Worker 优先路由本卡服务，保留模型/仿真 Python 隔离。
- smoke 为 CloseFridge、OpenDrawer 各1次、最多32步；full 保留 target50×50 官方 seed/horizon。
- smoke 开启完整 trace 便于验证，full 使用摘要；视觉 verifier 不替代环境 success。

## 已验证

- 本地20项测试通过，包含原有分层系统和 Qwen HTTP Tool 回归、新 Provider 的图像构造/JSON/重试/session，以及单样本和 batch=2 的真实进程 Graph 测试。生成器使用替身，不是真实权重。
- 服务器 model-runtime：torch 2.6.0+cu124、transformers 4.57.1。已用现有 `/data/models/Qwen3-VL-4B-Instruct` 的真实 processor 在 CPU 上处理图片，产生 input_ids、pixel_values 和 image_grid_thw。
- 修复 Transformers 4.57.1 对消息 content 列表的要求。
- 已在服务器独立目录 `/path/to/deployment` 准备 smoke 的 deployment、backend 和 execution JSON；没有加载 GPU 模型。

## 真实 GPU 验证更新

用户停止纯 VLA 全量后授权立即验证，再启动分层全量。原等待脚本不再使用。

- real-smoke：2/2 completed，0 errors/cancelled，每项32个环境步；Qwen planner4次、verifier2次、0 JSON重试，每次3路相机。服务关闭后session为0。
- batch-validation：16/16 completed，0 errors/cancelled，无重复episode，每项32步；Xiaomi32请求/17批，Qwen48请求/19批，两者最大实际batch均为6，配置上限8、等待50ms。
- 本地 Provider 与通用 batching 共31项测试通过。同卡路由修改后 Provider 的5项测试通过，新增同GPU两服务路由检查。
- 以上32步 smoke 均未达到环境成功，不能将链路验证结果解释为正式成功率或准确率对齐。
- 用户最新部署要求：8张卡每卡常驻一个 Xiaomi 与一个 Qwen，每卡16仿真Worker，batch8、50ms。先运行 colocated-validation，检查容量和服务生命周期，再启动 full-target50x50。
- trace summary 在并发验证启用，Xiaomi历史缓存auto按窗口关系关闭；不强制启用不适合当前replan间隔的缓存。

服务器证据：`/path/to/deployment/validation-evidence.json`。

## 最终目录组织

按用户要求将 Xiaomi/Qwen 实现移动到系统 tool/，Module 放在 module/，Graph 保留 configs/graphs.json。不创建 graph/ 目录，删除专用 run.py，统一 evaluate 加载 system.qwen_batch.json 和 execution.xiaomi_qwen.json。移动前后的模型实现保持不变，相关协议与系统测试重新验证。

## 同卡验证与全量启动

- colocated-validation：同卡 Xiaomi/Qwen、16 Worker，16/16完成，0错误，真实合批最大Xiaomi5、Qwen6；session及服务正常关闭。
- forced-batch8-capacity：仅测试适配器补齐每次forward到8样本，保持16仿真Worker。16/16完成，0错误，Xiaomi真实forward记录全部batch8。测试适配器不进入正式部署；此测试不用于准确率比较。
- 同卡测试曾观察到40192MiB显存，接近40GB容量；短测不能保证所有任务场景和更长文本都不会超过峰值，需要持续观察正式运行。
- 新目录服务器回归15项通过。本地系统15项通过，协议和后端24项通过、1项因本地缺torch跳过。
- 全量配置示例：8卡每卡Xiaomi+Qwen，128Worker，batch8、50ms；通过统一evaluate加载JSON Graph。使用官方任务seed/horizon，结果属于分层系统，不是纯Xiaomi成绩。
