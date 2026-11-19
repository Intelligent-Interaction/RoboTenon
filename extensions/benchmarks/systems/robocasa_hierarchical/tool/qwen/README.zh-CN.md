# Qwen3-VL 高层规划器

[English](README.md) · [**简体中文**](README.zh-CN.md)

真实模型后端，供分层系统的 HLP 使用；通过框架 `BatchProvider` 接口接收结构化请求，不是输出机器人 action 的 VLA。多个请求通过一次 `model.generate()` 批量推理。当前 RoboCasa 系统关闭视觉 verifier：每段 Xiaomi action 执行后先检查环境原生 success/terminal，未结束则直接进入下一次 HLP。

默认 Provider 支持本地 **Qwen3-VL Instruct** checkpoint。Qwen3.5 使用下方独立 Provider 和推理环境；两种架构不会互相当作兼容权重加载。模型版本记录应与 checkpoint 一起保存。

## 安装与资产

仅在模型 Python 环境安装依赖：

```bash
/model/python -m pip install -r extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen/requirements.txt
/model/python -m pip install -e extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen
```

权重需要完整的 config、processor/tokenizer 文件及 safetensors shards。服务器已有路径可直接使用；否则在目标服务器直接下载，例如：

```bash
hf download Qwen/Qwen3-VL-4B-Instruct --local-dir /models/Qwen3-VL-4B-Instruct
```

后端使用 `local_files_only=True`，运行期间不会自动下载或执行仓库远程代码。已检查服务器依赖为 torch 2.6.0+cu124、transformers 4.57.1，并实际验证该 checkpoint 的 CPU 图像 processor。已完成单样本、分卡及同卡动态 batch 真模型32步 smoke，均无错误；不代表正式任务成功率。

## 接口与配置

工厂为 `robotenon_qwen_reasoner.backend:Provider`。

参数：`model_path`、`max_new_tokens`（默认256）、`max_response_retries`（默认2）、可选 `metrics_dir`。

请求由公共 `benchmark_model` Tool 提供：

- `purpose`：`planner` 或 `verifier`。
- `prompt`、`system_prompt`：Graph Module 构造的任务、STM 和 JSON schema。
- `observation.images`：当前环境的 uint8 HWC RGB 图像。保持 native 观测方向，不额外裁剪、翻转或做 Xiaomi 专用处理。planner 按 session 缓存上次调用的画面，并将前后画面明确标为 previous/current；首次调用只有 current。
- `task` 与 episode metadata：来自实际 benchmark task。

生成使用 BF16、SDPA 和 greedy decoding。检测到完整顶层 JSON 后立即结束对应样本，避免模型在有效对象后继续输出分析并耗尽 token。批量生成后逐 request_id 返回 JSON；Tool 只校验完整 JSON 对象，planner 字段的缺省和类型归一化交给 PlanModule。JSON 语法错误或截断只重试失败项并保持原提示词；只有截断时 token 预算翻倍，最多初始值4倍，达到重试上限则报错。

每个 session 保存任务 metadata 和一份上次 planner 画面，episode 结束一并释放。`qwen-results/<pid>.json` 包含 planner/verifier 次数、模型 forward 次数及最近8条输出、最近16条失败原文（每条最多16384字符）。当前配置中 `verifier` 应始终为 0；不无限保存请求历史。

## 运行

优先使用 [分层系统的统一 Graph 入口](../../README.zh-CN.md#使用统一入口启动-graph)，无需手动启动 HTTP 服务。Qwen 服务和 Xiaomi 服务由同一个统一评测生命周期管理，仿真 Python 环境不导入模型依赖。

本次 af74484a 契约修复已通过本地回归；服务器连接中断，修复后的真模型结果尚未验证。此前 GPU smoke 结果属于修复前版本。

## Qwen3.5-9B 视觉语言模型

使用 `robotenon_qwen_reasoner.backend_qwen35:Provider`，显式要求 checkpoint 的 `model_type=qwen3_5`。复用相同的图像输入、JSON 解析、选择性重试、session 和动态 batch 契约。它是视觉语言模型，模型名称为 `Qwen/Qwen3.5-9B`。

将 `requirements-qwen35.txt` 安装到 Qwen 专用 Python 环境，保留 Xiaomi 所用的 Transformers 4 环境。通过框架现有独立 batch 服务进程运行 Qwen，无需让仿真器导入它的依赖。

```bash
/opt/envs/qwen35/bin/python -m pip install -r extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen/requirements-qwen35.txt
/opt/envs/qwen35/bin/python -m pip install -e . \
  -e extensions/benchmarks/systems/robocasa_hierarchical/tool/qwen
```

复制 [reasoner.qwen35.json](../../configs/reasoner.qwen35.json) 到本地配置目录并填写权重路径，再启动：

```bash
/opt/envs/qwen35/bin/python -m robotenon.runtime.batch_services \
  --config /data/configs/reasoner.qwen35.json \
  --ready-file /data/runs/system/reasoner-ready.json \
  --metrics-dir /data/runs/system/qwen-batch-metrics \
  --startup-timeout 1800 --shutdown-timeout 30
```

运行前创建输出父目录。`--config` 指向服务配置；`--ready-file` 在所有模型就绪后写入 socket 地址，选择该评测专用文件；`--metrics-dir` 保存合批统计，也应使用独立目录。启动超时 1800 秒为权重加载留余量，关闭超时 30 秒为正常退出留余量，可按实际加载和清理时间调整。

配置中的 `devices` 是模型 GPU 列表，单卡验证先用 `["0"]`；当前吞吐配置使用 `max_batch_size=32` 和 `max_wait_ms=500`。等待窗口只限制 GPU 空闲时的额外聚合时间，不包含等待前一个 forward 的排队时间。正式进展协议只返回 `status` 和短子目标，默认 `max_new_tokens=256`；完整 JSON 通常会提前结束，截断会触发现有重试机制并逐次扩大预算。

在系统 `execution-config` 中移除托管的同名 reasoner 服务，改填 `external_batch_services.reasoner`。它接受一个共享地址或按 Worker 顺序排列的地址列表；多 GPU 时按 `model_devices` 交错顺序重复 `workers_per_device` 次，将每个 Worker 连到本卡 reasoner。地址来自 ready JSON，不应硬编码临时 socket。详细生命周期与路由见 [系统接入](../../../../SYSTEMS.zh-CN.md)。评测结束后正常停止独立 Qwen 服务。

该入口的架构选择和共享契约有单元测试；真实权重、同卡容量和完整系统 rollout 应在目标硬件上验证，不能用接口测试代替准确率测试。

### 可选的模型推理模式

Provider 参数 `enable_thinking` 默认为 `false`。对于支持 thinking 的权重，可设为 `true`，
并相应增加 `max_new_tokens`（例如从 1024 开始验证）。此开关仅影响推理，不训练或更新模型权重。
启用后，JSON 完成检测只检查 `</think>` 后的最终回答，推理过程中的 JSON 示例不会提前终止生成；
未完成推理的输出按截断处理。传给 Graph 的仍然只有最终 JSON。

此模式会增加时延和 token 开销，其 RoboCasa365 成功率收益尚待 GPU 对照验证。
不要仅因启用 thinking 就将结果标记为更准确。
