# 评测由多个模型组成的系统

[English](SYSTEMS.md) · [**简体中文**](SYSTEMS.zh-CN.md)

[评测首页](README.zh-CN.md) · [并行与运行参数](EVALUATION.zh-CN.md) · [VLA 接口](models/README.zh-CN.md) · [RoboCasa 系统示例](systems/robocasa_hierarchical/README.zh-CN.md)

## 1. 选择接入方式

- **单个 VLA**：继续传 `--backend /path/to/vla.py`，入口自动生成默认 rollout Graph。
- **HLP + VLA + verifier 等完整系统**：额外传 `--system-config`，使用框架原生 Module / Tool / Graph。

两种方式复用 benchmark 的 Environment、Body、TaskAdapter、episode 队列和结果统计。系统可以放在本仓库 `systems/`，也可以是仓库外的可安装 Python 包。系统使用的模型加载、预处理和推理实现放在系统的 `tool/` 下；权重使用配置指向的外部目录。

系统 Graph 当前只支持 **sync profile**，不接受 live profile。Kinetix realtime 等需要专门的实时 Graph，默认单模型实时入口不受影响。

## 2. 目录和职责

```text
extensions/benchmarks/
  robocasa365/                   # 环境、动作规格、任务和协议
  benchmark-support/            # 统一入口和评测适配
  models/
    fake_reasoner/              # 测试用 HLP/verifier Provider
  systems/
    robocasa_hierarchical/
      pyproject.toml
      SOURCE.json               # 上游 Graph 版本与迁移说明
      tool/
        xiaomi/                 # 动作模型、预处理、batch 推理
        qwen/                   # 规划/验证模型、batch 推理
      configs/
        system.json             # 原生 Module/Tool 配置
        graphs.json             # 原生 Graph 配置
        execution.fake.json     # fake 模型和执行资源
        system.qwen.json        # 复用框架 Qwen API Tool
        execution.xiaomi.json   # Xiaomi 模型和执行资源
      module/robotenon_robocasa_system/
        modules.py              # 系统决策/适配逻辑
```

| 层 | 管理的内容 | 不应放入的内容 |
| --- | --- | --- |
| benchmark/profile | 任务集合、seed、horizon、环境 success、统计维度 | 系统的规划策略 |
| system config / Module | 条件分支、子目标、STM、模型结果处理 | 权重加载、手动拼 batch |
| Tool | 把 Module 请求送到模型服务、返回结构化结果 | 仿真成功率判定 |
| model Provider | 预处理、真实 batch 推理、模型 RNG、session 缓存 | Graph 控制流程 |
| execution config | Worker/GPU、batch 上限、等待时间、模型服务配置 | 覆盖 benchmark 任务协议 |

系统配置沿用框架的 `graphs_config`、`modules_config`、`tools_config` 等引用方式。相对 manifest 路径按系统配置目录解析。`parameters` 中可以放系统自己的参数命名空间。

评测入口保护 Environment、Body、TaskAdapter、task、environment、benchmark_protocol 等字段，系统配置不能替换它们。`parameters.runtime` 仅允许 `graph_id` 和 `monitor_enabled`。选择任务、episodes 或 seed 应修改 profile 或使用评测入口明确支持的协议覆盖。

## 3. 配置与命令

完整示例见 [RoboCasa 系统说明](systems/robocasa_hierarchical/README.zh-CN.md)。通用命令：

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile smoke \
  --system-config /path/to/system/configs/system.json \
  --execution-config /path/to/system/configs/execution.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --output runs/my-system-smoke
```

`--execution-config` 可省略；也可显式传 `--backend` 等 CLI 参数。CLI 覆盖 execution JSON 中的同名值，JSON key 使用下划线，例如 `batch_size`、`wait_ms`。后端和后端配置文件的相对路径按 execution JSON 所在目录解析；后端配置内部的 checkpoint 等路径由模型处理，推荐绝对路径。

execution JSON 可包含：

- 现有执行/模型参数，例如 `backend`、`backend_config`、`model_devices`、`workers_per_gpu`、`batch_size`、`wait_ms`、`max_retries`、`scheduler_seed`。
- `batch_services`：框架已有的 `BatchServiceConfig` 列表，用于额外模型服务。
- `external_batch_services`：已经启动的模型服务 socket 地址。

`benchmark`、`profile`、`output`、`system_config`、任务筛选和 episodes 不放入 execution JSON。

主 VLA 保持现有后端接口，其服务名称是 `vla`，Tool ID 是 `policy`。系统配置不能重复定义这个 Tool 或服务。其他 Tool 和服务可以任意命名；Tool 的 `params.service` 必须指向已配置的服务。

## 4. 动态 batch 对 Graph 透明

各 episode 独立执行 Graph，只有实际调用模型 Tool 才产生请求：

```text
Task A: plan    → planner 队列
Task B: action  → vla 队列（缓存 action 足够时不发请求）
Task C: verify  → verifier 队列
Task D: step    → 推进仿真，不调用模型
```

队列按服务、兼容 batch key 分组。满足 batch 上限或等待时间到期后执行，可发送不足上限的小 batch。不要求 task 的 step 相同，也没有跨 task 的同步屏障。

- `batch_size` / `wait_ms` 控制入口管理的 VLA。
- 其他服务分别使用 `max_batch_size` / `max_wait_ms`，不继承 VLA 数值。
- 同一服务的不同 GPU 副本各有队列，当前不是跨所有 GPU 的统一推理队列。
- Worker 优先绑定同 GPU 的模型副本；没有同 GPU 副本时按 Worker 序号轮转选择。
- Graph 中调用 Tool 的语义不变；单样本和 batch 使用同一 Graph。
- API Tool 的 batching 由 API 服务端决定，不能把客户端并发描述成模型的原生 batch。

同步环境等待决策期间暂停推进。`max_wait_ms` 是组 batch 的等待参数，不是端到端延迟保证；排队、前一个 batch 和推理时间仍会增加延迟。更改 batch 或调度可能改变模型浮点结果和全局 RNG 消耗顺序。

## 5. 接入 HLP / verifier 模型

VLA 继续使用独立 `vla.py` 的 `SPEC`、`create()`、`act_batch()` 接口。HLP / verifier 直接实现框架已有的通用 Provider 契约，无需中央注册表：

```python
class Provider:
    def __init__(self, model_path):
        # 每个服务副本只加载一次权重。
        self.model = load_model(model_path)
        self.sessions = {}

    def open_session(self, session_id, metadata):
        self.sessions[session_id] = metadata

    def infer_batch(self, requests):
        inputs = [r['observation'] for r in requests]
        outputs = self.model.infer_batch(inputs)  # 模型真正的 batch 接口
        return [
            {'request_id': r['request_id'], 'output': out}
            for r, out in zip(requests, outputs)
        ]

    def close_session(self, session_id, result):
        self.sessions.pop(session_id, None)

    def close(self):
        self.sessions.clear()
```

这是接口示意，`load_model` 及 `infer_batch` 由模型实现。把该包安装到模型环境，然后配置：

```json
{
  "batch_services": [{
    "name": "planner",
    "factory": "my_planner.backend:Provider",
    "params": {"model_path": "/models/planner"},
    "devices": ["0", "1"],
    "mode": "batch",
    "max_batch_size": 8,
    "max_wait_ms": 50,
    "max_pending": 256
  }]
}
```

系统中的 Tool 绑定该服务：

```json
{
  "plugin_tools": ["robotenon_benchmark_support.model_tool"],
  "tools": [{
    "id": "planner",
    "name": "benchmark_model",
    "kind": "model",
    "params": {"service": "planner", "timeout_s": 600}
  }]
}
```

`benchmark_model` 向 Provider 传入 Module 的 prompt 等参数，以及原生 images/state 观测：

```text
request.observation = {
  task, prompt, system_prompt, image_roles, ...,
  observation: {images, state, task_description, ...}
}
```

观测字典字段以 benchmark 为准。`image_roles` 是请求提示，通用 Tool 不自动把所有 benchmark 相机映射成外部/腕部相机；模型适配需明确选择对应相机。

每个请求带 `request_id`、`session_id`、`step_id`。返回可以乱序，但必须每个 request ID 恰好一次；框架检查缺失、重复和错误 ID。`open_session` 的 metadata 包含 benchmark、稳定 episode ID、task、action_spec 和 observation_spec。不要把不同 episode 的 KV cache 或 STM 混在一起。

同一模型存在不同输入形状/解码配置时，实现可选 `batch_key(request)`，返回可哈希的兼容性标识。只支持单样本的通用 Provider 必须配置 `mode: single`、`max_batch_size: 1`；通用 Provider 没有 VLA 的 SPEC 自动校验，不允许将逐样本串行调用宣称为 batch 加速。

框架在 episode 正常结束、异常或取消时调用各 Tool 的 `end_session()`；服务连接异常关闭时也会回收 session。用户自定义 Tool 应实现这个生命周期钩子。Module 实例可能复用，episode 状态应保存在 Graph workspace 或以 session ID 隔离的模型缓存中。

## 6. Python 环境和服务生命周期

- `--model-python`：协调进程和本次管理的所有模型服务使用此解释器。
- `--sim-python`：Worker、系统 Module / Tool、benchmark 环境使用此解释器。
- 模型权重包不导入仿真进程；不要通过注入另一个环境的 site-packages 解决依赖。
- 系统包安装在仿真环境，Provider 包安装在模型环境；框架和 benchmark-support 两边均需安装。

若 HLP 和 VLA 必须用不同 Python 环境，可用现有独立服务命令：

```bash
/envs/hlp/bin/python -m robotenon.runtime.batch_services \
  --config /absolute/path/hlp-services.json \
  --ready-file /absolute/path/hlp-ready.json \
  --metrics-dir /absolute/path/hlp-metrics
```

`hlp-services.json` 是 BatchServiceConfig 的 JSON **列表**。ready 文件包含 `服务名 -> [[GPU, socket路径], ...]`。把其中的 socket 路径配置到评测 execution JSON：

```json
{
  "external_batch_services": {
    "planner": ["/actual/socket/from/ready-file.sock"]
  }
}
```

单地址供所有 Worker 使用；多个地址必须按实际 Worker 顺序提供，每 Worker 一个。Worker 顺序是 GPU 交错：0,1,...,7,0,1,...,7。外部服务不得与管理中的服务重名，也不能覆盖入口管理的 `vla`。

独立启动的服务由启动者负责停止；评测入口只管理它自己启动的服务。这些本地 Unix socket 服务必须位于同一台主机。不同 GPU 上服务可独立分配设备，部署者需避免模型和渲染显存超额。

## 7. 结果与排查

输出仍包含逐 episode、逐任务、各维度和总体成功率。新增 `protocol.json.system_config` 保存解析后的系统配置，`model_services` 保存模型服务配置；`config.json` 和伴随 Graph/Module manifests 保存最终运行配置。

- `simulation.log`：Graph、Tool、环境错误。
- `batch-metrics/<service-index>-<replica-index>.json`：各服务请求数、batch 分布、等待时间等；索引顺序见 `model-service.json`。
- 服务 `sessions == 0`：关闭后没有存活会话。
- verifier 请求少于 VLA 很正常：缓存动作尚未执行完时会跳过验证。
- 第一 action 成功时，迁移示例直接结束，不再调用 verifier。
- `success` 由环境判定，verifier 的 `subtask_success` 不直接算作评测成功。

对比结果需记录系统 Graph、模型、协议、RNG 和执行参数。带 HLP/verifier 的系统评测不等同于 Xiaomi 官方纯 VLA evaluator，即使使用相同 target50 × 50 环境协议。
