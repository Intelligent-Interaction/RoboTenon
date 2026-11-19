# RoboCasa 分层系统：HLP → VLA

[English](README.md) · [**简体中文**](README.zh-CN.md)

[系统接入手册](../../SYSTEMS.zh-CN.md) · [RoboCasa365 安装与资产](../../robocasa365/SETUP.zh-CN.md) · [RoboCasa365 协议](../../robocasa365/README.zh-CN.md)

## 来源和边界

迁移来源：提交 `af74484a88d9fddd225e35ed211f50c8103172b3` 的
`extensions/robocasa365/configs/robotenon_xr365_graphs.json`。

保留两个 Graph：

- `xr365_single_task_reference`：默认，无 monitor 节点。
- `xr365_single_task`：包含 `monitor.check`；若使用监控器，在系统配置中明确配置 monitors 并启用 monitor。

保留原 Graph 的子目标指令、STM、验证和动作队列分支。正式 Qwen 配置使用系统内的通用进展规划 Module；参考 `plan` / `verify` Module 仍保留用于兼容和对照。

环境、Body、TaskAdapter **全部来自当前 `robocasa365_benchmark`**。没有恢复旧版 environment.py、body.py、tasks.py、Xiaomi socket Tool 或旧 horizon=16 配置。

迁移调整：

1. 每个 Graph action 节点执行一个原生 action；Tool 缓存模型返回的 chunk。这样缓存执行期间的每一步观测也会进入 VLA history。
2. `needs_plan=False` 时沿原 Graph 继续动作，verify 跳过模型调用；队列耗尽后才验证、重规划。
3. observe 后增加 `terminal_guard`，环境成功或到 horizon 时立即 END，不再调用 verifier/HLP/VLA。
4. 输出当前 benchmark 的逐 episode 指标和维度。子任务视觉验证不能代替环境 success。
5. Graph 的节点执行安全上限提高为 1,000,000；仿真的步数上限仍由 profile 和任务自己的 horizon 决定。
6. planner stop 仍要求环境确认成功；保留原分支，正常成功由 terminal_guard 提前结束。
7. 正式 Qwen 配置不使用任务名单或任务专用规则。STM 保存当前子目标；默认连续执行3个 action chunk 后才比较前后规划画面。`continue` 精确复用子目标，只有 `advance` 或 `recover` 可以替换它。

这些是明确的适配差异，不能宣称迁移后每个 Graph 节点、图像轨迹或模型调用时间与旧代码逐项相同。源提交和变更范围记录在 [SOURCE.json](SOURCE.json)。

## 安装

以下命令在 RoboTenon 仓库根目录执行。先按 RoboCasa365 SETUP 安装仿真和下载资产。模型环境和仿真环境分别安装：

```bash
# 模型环境：公共框架及测试用 Provider。
/envs/model/bin/python -m pip install -e . \
  -e extensions/benchmarks/benchmark-support \
  -e extensions/benchmarks/models/fake_reasoner

# 仿真环境：公共框架、当前环境和系统 Modules。
/envs/sim/bin/python -m pip install -e . \
  -e extensions/benchmarks/benchmark-support \
  -e extensions/benchmarks/robocasa365 \
  -e extensions/benchmarks/systems/robocasa_hierarchical
```

使用同一个 Python 环境时只需合并安装一次。此系统包本身不下载资产或权重。

## 第一次运行：三模型 fake smoke

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile smoke \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.fake.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --model-devices 0 \
  --output runs/robocasa-system-fake-smoke
```

配置使用零动作 VLA、fake planner 和 fake verifier；用于检查图像/请求/动作/统计链路，不用于判断系统能力。RoboCasa 的渲染仍需要可用 GPU；省略 model-devices 只是不绑定指定卡，不会取消仿真的 GPU 要求。

`execution.fake.json` 默认 2 个 Worker、VLA batch size 2、50 ms 组 batch 时间。fake planner/verifier 各有独立服务，默认不指定 GPU。fake VLA 的 replan_steps=1，因此每步都会验证；真实 Xiaomi 的 replan_steps=16 会启用缓存动作分支。

## 托管真模型版本（推荐）

现在提供完整的 **Xiaomi + Qwen3-VL** 模型服务接入：统一 evaluate 加载系统 JSON 和 Graph，常驻加载两个模型。正式配置不调用独立视觉 verifier；每一步先使用环境原生 success/terminal 判定，action chunk 用尽后复用当前子目标或按配置间隔调用 HLP 检查进展。

文件入口：

| 文件 | 用途 |
|---|---|
| `configs/system.qwen_batch.json` | HLP planner → Xiaomi action chunk → 环境终止检查 |
| `configs/smoke.real.json` | 两个任务各1次、32步 smoke；可覆盖多个16步 action chunk |
| `tool/qwen` | 真实 Qwen3-VL 模型 Provider，位于当前系统 tool 下 |

**验证状态：**已完成真实 Xiaomi + Qwen 两任务 smoke（2/2、0错误）和分卡16 episode并发验证（16/16、0错误），两模型均观察到实际多样本合批。测试上限32步，用于链路验证，不代表任务成功率。同卡双模型容量验证另行记录。

先安装上文框架、benchmark 与系统包，以及 [Qwen 模型依赖](tool/qwen/README.zh-CN.md) 和 [Xiaomi 模型依赖](tool/xiaomi/README.zh-CN.md)。分别在各环境安装所需包，不跨环境注入 site-packages。

### Module / Tool / Graph

| 层 | 声明或实现 | 职责 |
|---|---|---|
| Module | `module/robotenon_robocasa_system/modules.py` | 通用 keep/advance/recover plan、action、terminal；verify 节点在正式 Qwen 配置中只做无模型转发 |
| Tool | `configs/system.qwen_batch.json` 中的 planner；统一入口生成的 policy | HLP 通过 benchmark_model 调用 Qwen；policy 通过 benchmark_backend 调用 Xiaomi |
| Graph | `configs/graphs.json`，由 system.qwen_batch.json 引用 | reset → plan → action → body_execute → observe → terminal_guard；chunk 用尽后进入 plan，plan 可直接复用子目标而不请求 Qwen |
| 服务部署 | `configs/execution.xiaomi_qwen.json` | 每卡各一份 Xiaomi/Qwen，16 Worker，batch 上限8、等待50ms |

Tool 复用 benchmark-support 的通用实现，不在系统目录复制模型 RPC 和历史管理。模型权重及推理实现位于 models；系统通过 Module / Tool / Graph 组合它们。

### 使用统一入口启动 Graph

先修改 `tool/xiaomi/config.json` 的 checkpoint 路径，以及 `configs/execution.xiaomi_qwen.json` 中 Qwen 的 `model_path`。下面路径均相对于仓库根目录。

```bash
Tenon evaluate \
  --benchmark robocasa365 \
  --profile extensions/benchmarks/systems/robocasa_hierarchical/configs/smoke.real.json \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.qwen_batch.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.xiaomi_qwen.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/robocasa/bin/python \
  --output runs/xiaomi-qwen-smoke
```

该命令由统一 evaluate 加载 Graph，自动启动模型服务和仿真，不需要专用 runner。默认部署配置使用8卡；单卡验证时，同时将 execution 中 `model_devices` 改为 `"0"`、reasoner 的 `devices` 改为 `["0"]`。两者相同的 GPU 绑定让 Worker 使用本卡两项服务。也支持配置不同设备集合分卡部署。

检查 `runs/evaluation/summary.json`：smoke 应完成2个 episode、0 errors。检查 `batch-metrics` 的真实批大小与结束后的 sessions=0。两任务不足以验证 batch8 容量，可通过 `--episodes-per-task 8` 增加并发验证量。需要检查 Qwen 输出时，在 reasoner params 中配置 `metrics_dir`。

验证通过后，将 `--profile` 换成 `xiaomi_official`，输出目录改为新目录，运行 target50 × 50。full 使用官方任务 horizon 和 seed，不使用32步上限。这是 **Xiaomi + Qwen 分层系统成绩**，不是 Xiaomi 官方纯 VLA 成绩。记录实际 GPU、batch、RNG 配置；32步 smoke 的成功率不能用作正式成绩。

系统没有专用 runner；统一 evaluate 负责资源生命周期，Module / Tool / Graph 定义运行逻辑。

## 可选：外部 Qwen HTTP 服务模式

准备：

1. 安装 [Xiaomi 模型依赖](tool/xiaomi/requirements.txt)，下载 RoboCasa365 checkpoint；在独立 backend JSON 中填写实际 `model_path` 和 RNG 模式。
2. 在另一个进程启动提供 Qwen 视觉语言模型的 OpenAI-compatible chat completions 服务。
3. 设置 `QWEN_ENDPOINT`、`QWEN_MODEL`；需要认证时通过 `QWEN_API_KEY` 环境变量提供，不写入配置或报告。
4. 按 RoboCasa365 SETUP 准备相应资产，并先运行少量任务。

提供的 `system.qwen.json` 使用框架现有 `Qwen3.5-VL` Tool 处理规划，VLA 仍使用统一服务；视觉 verifier 默认关闭。示例：

```bash
export QWEN_ENDPOINT=http://127.0.0.1:8000/v1/chat/completions
export QWEN_MODEL=/models/Qwen3.5-9B

Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.qwen.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.xiaomi.json \
  --backend-config /absolute/path/xiaomi-backend.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --model-devices 0 --workers-per-gpu 2 --batch-size 2 \
  --max-tasks 2 --episodes-per-task 1 \
  --output runs/robocasa-system-real-smoke
```

Qwen API Tool 在仿真进程运行 HTTP 客户端，不加载模型权重。它从观测缓存取图片并发送；API 服务端是否 batching 由服务端决定。本配置只保证 Xiaomi VLA 使用本框架动态 batching，不宣称 Qwen 客户端在做 batch 推理。

若要三个模型都使用本框架的 batching 服务，使用 `system.json`，将 `execution.fake.json` 中的两个测试 Provider 换成真实 HLP/verifier Provider，VLA backend 换成 Xiaomi。接口见 [通用 Provider 说明](../../SYSTEMS.zh-CN.md#5-接入-hlp--verifier-模型)。不能用 fake HLP/verifier 的结果作为完整真模型系统成绩。

### 全量协议与 8 GPU 配置

确认真实模型 smoke 后，移除 `--max-tasks`、`--episodes-per-task` 和 smoke 的 GPU/Worker 覆盖：

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.qwen.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.xiaomi.json \
  --backend-config /absolute/path/xiaomi-backend.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --output runs/robocasa-system-target50x50
```

该 execution 配置是 8 个 VLA GPU 副本、每 GPU 16 个仿真 Worker、VLA batch size 8、组 batch 最大等待 50 ms。Qwen 服务需要另行分配足够资源；若与这 8 张卡共用显存，应先测容量。

环境协议仍是 target50 × 50、官方 seed 和任务 horizon。**这是分层系统的成绩，不是 Xiaomi 官方纯 VLA 成绩。** 想评测纯 VLA 时省略 `--system-config`，使用原统一评测命令。

## 修改自己的系统

- 改流程：编辑 `configs/graphs.json`，使用现有 Graph 条件边与 goto。
- 改规划策略：新增 Module，通过 plugin_modules 注册，然后在 Graph 中引用。
- 改模型：修改 Tool 服务绑定或 Provider factory，避免在 Module 内加载权重。
- 改推理资源：编辑 execution JSON；其他模型的 max_batch_size 不受顶层 batch_size 覆盖。
- 用不同 verifier：设置 `parameters.model.verify_tool`；不设置时复用 hlp_tool。
- 换 benchmark：用其他 benchmark/profile，但模型必须明确适配对应的观测与动作空间；本示例只把 RoboCasa365 作为目标环境，不承诺跨机器人直接可用。

## 本地验证范围

`tests/test_benchmark_systems.py` 使用真实框架进程、Graph、共享模型服务和确定性仿真契约 fixture，验证：

- 当前 benchmark 的 Environment、Body、TaskAdapter 和协议不被系统配置覆盖。
- 单样本和动态 batch 的系统流程一致。
- action chunk 缓存期间每步观测进入历史。
- 成功立刻终止，不额外调用 verifier。
- 任务在不同时间结束，未结束的任务继续领取/运行。
- 失败 episode 精确到 horizon；请求乱序返回不会串 session。
- 服务正常回收会话；模型没有被仿真进程导入。

这些契约测试不替代真实 RoboCasa 渲染、Xiaomi 权重推理或在线 Qwen 服务验证。真实 smoke 和正式评测需在具备资产/模型/GPU 的部署环境执行。

## af74484a 对齐回归

核心 planner/verifier 与该提交一致，Qwen Tool 只解析完整 JSON，字段处理由 Module 负责。保留现有 native benchmark 接口、逐步观测、成功后的立即终止和动态 batching；Graph JSON 中 terminal_guard 和指标输出是明确保留的 benchmark 适配。当前 Qwen3-VL-4B 与参考 Qwen3.5 是不同模型，因此这里的契约对齐不表示模型轨迹或成功率等价。

两任务 smoke 没有覆盖全量中出现的 planner 字段变体。复测使用 `configs/smoke.af74484a.json`，覆盖全部 target50，每项1次、最多64步。保持 `--system-config configs/system.qwen_batch.json`（实际传入完整路径）和统一 evaluate 入口；预期50条唯一episode记录、0 errors/cancelled。检查真实 batch 统计、planner/verifier调用、错误原文、会话关闭。64步测试只判断链路，不能用其成功率代替正式成绩。

此回归配置保留正式 seed stride=50，但不是正式 50×50 协议。部署时先验证真实模型、资产和渲染链路，再运行 xiaomi_official。

## 无需训练的上下文规划实验

`configs/system.contextual.json` 使用现有 Qwen 和 Xiaomi 权重，不增加训练或微调。
Qwen 根据原始任务、前后两组相机画面和最近执行的指令，生成下一条 Xiaomi 指令。
完整原始任务、当前指令和上下文相关的新指令均可由模型选择；不强制拆成原子动作。
该配置尚未证明优于纯 Xiaomi，适合用于配对实验，不应直接视为更高准确率的默认配置。

另一个实验配置 `configs/system.advisory.json` 保留完整原始任务作为 Xiaomi 输入的前缀。
Qwen 输出 `{"memory": "场景事实和不确定性", "guidance": null}`，或将 guidance 设为非空的视觉补充信息。
guidance 为 null 时，Xiaomi 收到原始指令；有补充时，输入为原始指令加换行再加补充信息。
Qwen 下一次可以撤回补充，恢复原指令。memory 仅传给 Qwen，不传给 Xiaomi，并随 episode 会话隔离。
此模式使用 `policy.planner_mode=advisory`，其余调用频率、Graph、动作历史和终止语义保持一致。
它没有任务专用分支、人工停滞阈值或训练步骤；保留原任务文本本身也不保证成功率不下降。
使用与其他系统相同的统一入口，将 `--system-config` 指向该配置即可运行实验。

- `policy.planner_mode=contextual`：启用上下文规划，输出结构为 `{"instruction": "..."}`。
- `policy.progress_check_interval_chunks=3`：每执行 3 个 action chunk 调用一次 Qwen。
  此参数控制推理频率，不判断停滞或成功；更小的值增加视觉反馈频率与推理开销。
- 最近 8 次规划间隔的指令和实际执行 chunk 数保存在 episode 的 STM 中。
  这些记录表示已执行的命令，不表示命令已成功。前一组规划图像对应最近一条记录执行前的场景。
- Xiaomi 在每次预测时仍获得最新观测和原有历史采样；环境 success 仍决定终止。
- 非法模型输出显式报错，不静默替换成另一条指令。

评估该配置时，使用相同的任务集合、episode seed、horizon 和模型设置重新运行纯 Xiaomi 基线。
使用 `session_deterministic` 和 VLA batch size 1 可以减少调度与 batch 数值差异的干扰；
这属于方法对照，不等同于 `official_stream` 正式成绩。先完成全部配对 episode，报告双方成功率、
系统独有成功和 Xiaomi 独有成功，再在独立 seed 集合验证。动态 batch 的吞吐与准确率另行验证。

该设计参考 [Inner Monologue](https://arxiv.org/abs/2207.05608) 的执行反馈思路。
论文使用其自身的技能与反馈模块，不构成 Qwen 与 Xiaomi 组合必然提升成功率的证据。

配对结果可使用统一的只读统计命令：

```bash
python -m robotenon_benchmark_support.paired_results \
  runs/xiaomi-baseline runs/xiaomi-contextual > runs/paired-results.json
```

两个参数是 evaluate 的输出目录。命令检查协议、模型参数和 episode 清单是否一致；
只有两组均完成全部计划 episode 后才计算成功率差异，并报告按任务聚类 bootstrap 的 95% 区间。
总体和 `per_task` 均报告 `both`（双方成功）、`system_only`（仅系统成功）、
`baseline_only`（仅基线成功）和 `neither`（双方失败），避免净成功率掩盖新增能力与退化。
`baseline_success_retention_rate` 是原本基线成功的 episode 中系统仍成功的比例；
`baseline_failure_recovery_rate` 是原本基线失败的 episode 中系统成功的比例。
分母为零时对应值为 `null`。这里的“恢复”仅描述配对结果，不代表系统执行了特定恢复动作。
`execution_differences` 列出 batch 和调度设置差异，便于识别额外混杂因素。
该区间衡量当前测试集的抽样不确定性，不能替代独立 seed 验证。
相同 seed 不保证上游仿真器生成完全相同的初始场景。统计命令只核对声明的协议和 episode 身份，
以 `initial_state_alignment=not_verified` 明确标记这一限制。严谨的配对实验还应检查初始图像、
机器人状态和场景元数据，并先用保持原指令的系统验证动作执行一致性。
若为排查随机性修改上游场景采样顺序，应单独标记诊断协议，不能将其成绩称为官方 evaluator 成绩。
Qwen 配置提供 `metrics_dir` 时，逐请求决策保存在每个服务的 `*.decisions.jsonl` 中，
记录 session、任务、提示词、模型输出与实际输入图片数量，便于定位错误；不写入图像或权重。
