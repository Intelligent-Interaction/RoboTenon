# Xiaomi + RoboCasa365：复现指南与实测参考

[English](benchmark-h100-xiaomi.md) · [**简体中文**](benchmark-h100-xiaomi.zh-CN.md)

这篇文档面向准备复现 Xiaomi + RoboCasa365 的读者，协议为 target50 × 50。除最终分数外，
本文还说明硬件容量、启动参数、统计口径和常见排错方法；安装与资产准备见
[Benchmark 使用指南](../benchmarks.zh-CN.md)和 [RoboCasa365 SETUP](../../extensions/benchmarks/robocasa365/SETUP.zh-CN.md)。
示例不会公开服务器、账号、私有目录、运行 ID 或文件哈希。

## 硬件与并行规模

| 项目 | 配置示例 | 如何选取 |
| --- | --- | --- |
| GPU | 8 × H100 80GB | 先单卡验证模型、推理和渲染显存，再扩展到多卡 |
| CPU | 96 物理核、192 逻辑 CPU | 根据仿真 CPU 开销与线程数选择 Worker 总数 |
| 内存 | 约 2 TiB | 为所有 Worker 的场景、纹理、reset 峰值留余量；这是容量参考，不是最低要求 |
| 模型副本 | 每卡 1 个，共 8 个 | 优先每卡一份；增加副本前比较吞吐与显存开销 |
| 仿真 Worker | 每卡 28 个，共 224 个 | 逐步增加，直到吞吐不再增长或资源压力上升；不要直接在小内存机器照用 |

全局队列为 Worker 分配 episode。各模型服务仅接收绑定 Worker 的请求并独立组 batch。固定环境 seed 并不保证不同调度下动作相同：模型 `official_stream` RNG 会受请求顺序、batch 和副本数量影响。

## 评测结果与并行效率

**8 张 H100 在 1 小时 46 分 42 秒内完成 2,500 个 episode，成功率 57.56%。** 端到端平均吞吐为 **23.43 episodes/分钟**；中段持续吞吐为 **26.97 episodes/分钟**。以下结果对应上面的 8 模型副本、224 Worker、batch 上限 8、50ms 配置，未保存视频。

| 指标 | 结果 | 统计口径 |
| --- | ---: | --- |
| 完成量 | 2,500 / 2,500 | 50 个任务，每任务 50 个 episode；无漏项、无重复身份 |
| 成功 / 普通任务失败 | 1,439 / 1,061 | 以最终 episode 的任务成功信号统计 |
| 总体成功率 | 57.56% | 1,439 ÷ 2,500；各任务数量相同，亦等于任务成功率均值 |
| 错误 / 取消 | 0 / 0 | 最终汇总中的 num_errors / num_cancelled |
| 完整耗时 | 6,401.88 秒，约 106.70 分钟 | evaluator 的 wall_sec，含模型服务启动、Worker 执行和服务关闭；不含资产下载和依赖安装 |
| 端到端吞吐 | 23.43 episodes/分钟 | 2,500 ÷ 完整耗时（分钟） |
| 中段持续吞吐 | 26.97 episodes/分钟 | 第 250 项完成至第 2,250 项完成，2,000 项用时 4,448.81 秒 |
| 模型请求 / batch 调用 | 206,674 / 88,766 | 8 个模型服务最终计数之和 |
| 实际平均 batch | 2.33 | 请求数 ÷ batch 调用数；配置上限 8 不等于实际平均值 |

### 吞吐提升来自哪些机制

- **并行推进**：224 个仿真 Worker 与 8 个常驻模型服务同时工作，模型权重按副本加载，避免按 episode 重复加载。
- **动态合批**：206,674 个请求合并为 88,766 次模型调用。相对于每请求一次调用的计数方式，减少 **117,908 次调用（57.05%）**，摊薄模型调用开销。这是调用次数减少比例，不是已测得的整体耗时加速倍数。
- **全局 episode 调度**：空闲 Worker 自动领取新 episode，减少任务长短不同造成的静态分配空闲。
- **控制公共开销**：线程池限制、trace 摘要和有界检查点控制高并发下的 CPU 与记录开销。历史增量缓存在此模型配置下关闭，不能将吞吐收益归因于它。

这些机制共同支撑了不到两小时完成完整协议的处理能力。当前没有相同条件下完成的 batch-size-1 全量对照，因此不声明相对官方 evaluator 或串行执行的整体加速倍数，也不把各优化单独归因到某个百分比。

### 结果适用范围

结果来自单次完整协议运行，未通过重复运行证明统计稳定性；动态 batch 和 `official_stream` 可能改变轨迹。使用的运行依赖与仓库默认安装版本存在差异，结果不构成对上游环境逐项完全一致的声明。中段吞吐排除了前后各 10% 的完成项，不能用于替代完整耗时；硬件、任务分布、观测规格或视频设置变化后需重新测量。

[下载公开结果数据](../assets/benchmark-results/robocasa365-xiaomi-h100.json)：包含协议、资源配置、50 个任务的成功数、完整计数和 batch 分布，不包含服务器地址、账户、私有路径或环境名。数据由最终 summary、episode 索引和模型服务计数汇总得到；所有 8 个服务均记录正常关闭。

## 模型与协议

在仓库根目录创建 `local-configs/xiaomi.json`，使用实际 checkpoint 路径替换示例路径：

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-1-RoboCasa365",
  "rng_mode": "official_stream"
}
```

| 配置项 | 含义 | 如何选取 |
| --- | --- | --- |
| `model_path` | 已下载模型 checkpoint 的目录 | 指向兼容 RoboCasa365 的权重；使用模型进程可见的绝对路径 |
| `rng_mode` | 模型随机数策略 | 官方随机流对照用 `official_stream`；调度性能对照可用 `session_deterministic`，报告中注明 |
| `target50 × 50` | 50 个任务，每任务 50 个 episode | 完整协议共 2,500 项；smoke 用任务/episode 覆盖，并与正式结果区分 |
| `split=pretrain` | 场景划分 | 沿用 profile；改变 split 后不属于相同协议 |
| `seed=7`、stride 50 | 环境 seed 为 `7 + 原始任务索引 × 50 + episode_idx` | 保留原始索引和协议 seed；不要用 Worker 编号生成 |
| `camera_size=256` | 环境图像宽高 | 正式评测保留 256，不用降分辨率换取加速 |
| 任务 horizon | 每任务允许的原生环境步数 | 使用任务原始值；success 后立即结束 |
| history `length=4`、`interval=2` | 模型输入 4 帧历史，采样间隔为 2 个环境步 | 由 Xiaomi 后端声明；换模型时按该模型训练/推理要求填写 SPEC |
| `replan_steps=16` | 每次推理最多消费 16 个 action 后重新规划 | 由后端声明；增大会改变控制行为，不能作为无损加速开关 |

## 全量启动命令

以下命令在仓库根目录执行，要求模型和仿真已按安装指南准备好。示例路径均为占位路径。`MODEL_PY` 指向模型解释器，`SIM_PY` 指向仿真解释器；需要额外渲染设置时也可以指向自己的可执行包装脚本。

```bash
export MODEL_PY=/opt/envs/model/bin/python
export SIM_PY=/opt/envs/robocasa365/bin/python
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false MALLOC_ARENA_MAX=2
export EVAL_OUTPUT="runs/robocasa365-xiaomi-$(date +%Y%m%d-%H%M%S)"

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./local-configs/xiaomi.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 28 --batch-size 8 --wait-ms 50 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --startup-parallelism 16 --recycle-after-tasks 5 --max-retries 1 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output "$EVAL_OUTPUT"
```

### 命令参数逐项说明

| 参数 | 含义 | 如何选取 |
| --- | --- | --- |
| `--benchmark robocasa365` | 使用 RoboCasa365 环境适配 | 需与模型支持的观测和动作空间一致 |
| `--profile xiaomi_official` | 加载完整 target50 × 50 协议 | 正式评测使用此协议；小规模检查添加下文的子集覆盖 |
| `--backend` | 模型工厂 `vla.py` | Xiaomi 使用示例路径；自定义模型替换为自己的文件 |
| `--backend-config` | 传给模型工厂的 JSON 参数 | 填写 checkpoint 与 RNG，独立于协议和资源配置 |
| `--model-python` | 协调器和模型服务解释器 | 选择安装模型依赖的 Python |
| `--sim-python` | 仿真 Worker 解释器/包装脚本 | 选择安装仿真依赖并能正确渲染的 Python |
| `--model-devices` | 参与推理和渲染绑定的 GPU 列表 | 只填可用设备；单卡 smoke 用 `0` |
| `--workers-per-device 28` | 每个设备的仿真 Worker 数 | 从 1、4、8 逐步增大，对比 episodes/min 和内存峰值；总数为设备数 × 此值 |
| `--batch-size 8` | 每个模型副本的 batch 上限 | 基线用 1；支持真实合批后试 2/4/8，确认显存和实际 batch 分布 |
| `--wait-ms 50` | 组 batch 等待窗口，单位毫秒 | 从 50 开始比较；单样本可用 0，过长窗口会增加动作等待 |
| `--scheduling-strategy global_queue` | 空闲 Worker 动态领取下一个 episode | 通常用此模式减少任务长短不均；固定分配对照用 static |
| `--scheduler-seed 7` | 生成初始 shuffle 顺序的独立随机种子 | 固定并记录任意整数；对照实验保持一致 |
| `--startup-parallelism 16` | 同时启动/reset 的并发上限 | 按初始化资源峰值选取；小机器先用 1–8，不必等于 Worker 数 |
| `--recycle-after-tasks 5` | 每 Worker 完成 5 次执行后重建 | 有内存增长时可用较小值；稳定环境可增大以减少重建开销 |
| `--max-retries 1` | WorkerLost 后允许重试一次 | 通常用 1；定位首次故障用 0，不会重试普通任务失败 |
| `--trace-mode summary` | 保存调用统计摘要和错误，减少逐节点 trace | 正常评测用 summary；调试完整时间线用 full |
| `--history-cache auto` | 自动判断是否缓存重复历史传输 | 通常保持 auto；Xiaomi 4/2/16 无相邻历史重叠，实际关闭 |
| `--context-history-limit 64` | 检查点保留的最近诊断事件数 | 默认 64；仅在需更长诊断窗口时增加，不影响模型 history |
| `--context-outputs-limit 64` | 检查点保留的最近节点输出数 | 默认 64；增大会增加检查点开销，不改变 action chunk |
| `--output` / `EVAL_OUTPUT` | 本次结果根目录 | 每次选择新目录；时间戳用于避免覆盖 |

`batch-size=8` 不保证每次都有 8 个请求，`wait-ms=50` 也不包含全部排队和推理延迟。shuffle 默认开启；`--no-shuffle` 用于关闭初始打乱。它不改变任务身份或环境 seed。

### 线程设置

| 变量 | 含义 | 如何选取 |
| --- | --- | --- |
| `OMP_NUM_THREADS` | OpenMP 线程池大小 | 大量 Worker 从 1 开始；低并发计算负载才尝试增加 |
| `MKL_NUM_THREADS` | MKL 数值库线程数 | 多进程通常先设 1，避免与其他线程池叠加 |
| `OPENBLAS_NUM_THREADS` | OpenBLAS 线程数 | 多进程通常先设 1，按总吞吐调整 |
| `NUMEXPR_NUM_THREADS` | NumExpr 表达式线程数 | 多进程通常先设 1 |
| `TOKENIZERS_PARALLELISM` | tokenizer 是否启用内部并行 | 多 Worker 先设 false；分词成为瓶颈时再做对照 |
| `MALLOC_ARENA_MAX` | glibc 内存分配 arena 上限 | Linux 可从 2 开始观察 RSS 和分配开销；其他分配器未必适用 |

这些变量不是进程总线程数、CPU 或内存的硬配额。更多参数（超时、虚拟 GPU、EGL 映射等）的默认值与选取建议见[完整 CLI 参数表](../../extensions/benchmarks/EVALUATION.zh-CN.md#8-完整-cli-参数表)。

## 先做 smoke，再评测完整协议

首次使用时，将命令改为单卡、单 Worker、batch 1、wait 0，并增加 `--max-tasks 1 --episodes-per-task 1`。前者只枚举一个任务，后者只执行一个 episode，适合检查动作、图像和终止；正式评测去掉这两个覆盖项并使用新 output。

长时间运行可在服务器持久终端会话中执行。例如先运行 `tmux new -s xiaomi-eval`，再在其中执行上面的命令；`-s` 是会话名，选择易识别且未占用的名称。

## 查看进度与最终结果

```bash
"$MODEL_PY" -m json.tool "$EVAL_OUTPUT/protocol.json"
"$MODEL_PY" -m json.tool "$EVAL_OUTPUT/runs/evaluation/status.json"
nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total --format=csv
```

`protocol.json` 用于核对实际协议、覆盖项与执行参数；`status.json` 提供运行中进度。`nvidia-smi` 的 query 列表分别查询设备编号、GPU 利用率、已用和总显存，CSV 便于对照各卡；瞬时利用率不能单独代表整体吞吐。

最近吞吐查询见[SQLite 进度示例](../benchmarks.zh-CN.md#5-查看进度速度和结果)。`batch-metrics/*.json` 提供实际 batch 分布，最终分数读取 `runs/evaluation/summary.json`，并核对 2,500 项完整计数及 errors/cancelled。

不要将中途成功率作为最终成绩：成功通常提前结束，失败可能执行到 horizon。估算剩余时间时采用稳定阶段的近期速度，并为尾部较长 episode 留余量。

## 常见错误和踩坑点

- **直接照抄 224 个 Worker**：这是 96 物理核、约 2 TiB 内存机器上的实测配置，不是默认值。
  新机器从低并发逐步增加；进程更多可能因为内存和 CPU 争用而更慢。
- **把 `completed` 当成成功**：正常跑到 horizon 的 episode 也是 completed，必须以
  `task_success` 统计任务成功，并同时报告 error / cancelled。
- **用中途成功率预测最终分数**：成功轨迹通常提前结束，失败轨迹更常跑满 horizon，因此
  前半程成功率容易偏高。最终只使用完整 2,500 项汇总。
- **为了减少失败统一延长 horizon**：这会改变 target50 × 50 协议。性能实验可以另建 profile，
  但不能把修改后的分数当成原协议结果。
- **把 batch 上限当成实际 batch**：配置 8 不代表每次调用都有 8 个请求。本轮实际平均 2.33，
  调优必须查看 `batch-metrics`。
- **忽略 RNG 与调度关系**：`official_stream` 会受请求顺序、模型副本数和 batch 影响。比较优化
  前后分数时需要保持资源配置与调度一致，或明确使用其他 RNG 模式做性能对照。
- **模型和仿真依赖混装**：RoboCasa / MuJoCo 与 Transformers / PyTorch 应放在独立环境中，
  通过 `--model-python` 和 `--sim-python` 连接。
- **开启视频后仍引用本文耗时**：本轮关闭录像。全量视频会增加编码与磁盘 I/O，只建议为少量
  失败任务开启。
