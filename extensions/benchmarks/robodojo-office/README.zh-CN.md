# RoboDojo 官方评测扩展

[English](README.md) · [**简体中文**](README.zh-CN.md)

仿真依赖与下载方式见 [RoboDojo 安装指南](../robodojo/SETUP.zh-CN.md)。该旧版集成使用下方专用命令和 Graph。模型和服务的前置条件需另行准备。


`robodojo-office`（名称按项目约定保留，含义是 **official evaluation**）把
RoboDojo 当作完整的官方 benchmark 使用。RoboDojo 继续拥有 Isaac Sim、reset、
step、成功判定、seed/layout、视频、故障恢复和结果目录；RoboTenon 作为 XPolicyLab
策略服务，并通过 TaskAdapter 启动官方 client、回收官方结果。

## 与 `robodojo` 的架构区别

```text
robodojo-office（本扩展，正式评测）
RoboTenon TaskAdapter -> 官方 robodojo.sh client -> Isaac Sim
                                      |
                                      +-- WebSocket -> RoboTenon policy server

robodojo（环境接管，研究/调试）
RoboTenon Graph -> EnvironmentSession/Body -> RPC -> Isaac Sim service
```

| 项目 | `robodojo-office` | `robodojo` |
| --- | --- | --- |
| 控制循环所有者 | RoboDojo | RoboTenon Graph |
| 官方评测语义 | 完整保留 | 不保证与官方 leaderboard 完全相同 |
| Graph 使用方式 | 策略侧推理 Graph/Executor | 完整 `reset -> action -> body -> observe` Graph |
| batch、多 GPU、恢复 | 沿用 RoboDojo | 第一版只支持单环境、单 Worker |
| 推荐用途 | 正式跑分、回归、排行榜对比 | 单步调试、安全控制、轨迹研究 |

两个扩展互不依赖，不会同时注册同名 provider。

## 代码边界

- `tasks.py`：枚举官方评测 case，并调用 runner。
- `runner.py`：构造无 shell 拼接的 argv、管理进程组和超时；用唯一 run id
  精确关联结果，绝不按“最新文件”猜测。
- `results.py`：保留 native result，同时投影为 RoboTenon 指标。
- `policy_bridge.py`：只维护 XPolicyLab 协议状态；真实模型/Graph 由显式注入的
  `PolicyExecutor` 负责。
- `server.py`：加载 `module:callable` executor factory，启动 XPolicyLab WebSocket。
- `deployment.py`：供 RoboTenon 托管 policy server 进程。
- `assets/xpolicylab_policy/RoboTenon/`：RoboDojo client 需要的薄 deploy shim。

没有隐式模型发现。生产 executor 必须通过 `--executor-factory module:callable`
显式配置，并实现 `reset()`、`infer()`、`trial_end()`。内置
`create_zero_executor` 只用于链路 smoke test。

## 1. 安装代码

```bash
python -m pip install -e .
python -m pip install -e 'extensions/robodojo-office[server]'
```

RoboDojo 建议放在 RoboTenon 仓库之外：

```bash
export ROBODOJO_ROOT=/data/RoboDojo
git clone --recurse-submodules \
  https://github.com/RoboDojo-Benchmark/RoboDojo.git \
  "$ROBODOJO_ROOT"
git -C "$ROBODOJO_ROOT" submodule update --init --recursive
```

当前适配基于 RoboDojo `main` 和 XPolicyLab WebSocket 协议。部署时应固定经过验证的
commit，而不是每次运行自动拉取最新代码。

## 2. 下载仿真资产

官方脚本从 Hugging Face dataset `RoboDojo-Benchmark/RoboDojo` 只拉取
`Assets/**`，需要 Git LFS：

```bash
git lfs install
cd "$ROBODOJO_ROOT"
bash scripts/init_assets.sh
```

完成后应存在：

```text
$ROBODOJO_ROOT/Assets/Robots
$ROBODOJO_ROOT/Assets/Object
$ROBODOJO_ROOT/Assets/Material
$ROBODOJO_ROOT/Assets/Eval_Layout
```

不要把 `Assets/` 提交进 RoboTenon。生产环境应记录资产 revision；可用
`HF_REVISION=<commit-or-tag> bash scripts/init_assets.sh` 固定版本。

## 3. 安装 Isaac Sim 评测环境

原生安装要求 Linux/NVIDIA GPU；官方当前安装脚本使用 Python 3.11、Isaac Sim
5.1、CUDA 12.8 相关依赖：

```bash
cd "$ROBODOJO_ROOT"
bash scripts/install.sh -i
```

也可以构建官方 Docker 镜像：

```bash
cd "$ROBODOJO_ROOT"
docker build -t robodojo:cuda12.8 .
```

官方文档提示镜像约 200 GB，建议至少预留 300 GB 磁盘，并使用支持 CUDA 12.8
的 NVIDIA 驱动。macOS 适合开发 adapter，不能运行这套 Isaac Sim 评测端。

## 4. 安装策略 shim

RoboDojo 的 `client` 命令会检查
`XPolicyLab/policy/<POLICY>/deploy.py`。把随扩展提供的 shim 复制到当前固定版本的
RoboDojo 源码目录。

```bash
cp -R extensions/benchmarks/robodojo-office/assets/xpolicylab_policy/RoboTenon \
  "$ROBODOJO_ROOT/XPolicyLab/policy/RoboTenon"
```

这两个文件不包含模型代码，只声明通用 `reset/update_obs/get_action` 循环。

## 5. 模型权重和训练数据

RoboDojo 没有一个适用于所有策略的统一 checkpoint。每个 XPolicyLab adapter
独立声明模型仓库、权重目录和安装脚本，因此必须以
`$ROBODOJO_ROOT/XPolicyLab/policy/<POLICY>/README.md` 为准：

```bash
cd "$ROBODOJO_ROOT/XPolicyLab/policy/<POLICY>"
bash install.sh
# 按该策略 README 下载 checkpoint，并填写 deploy.yml 或指定 checkpoint 路径。
```

如果需要训练/离线调试数据，而不只是在线评测，可下载官方转换数据：

```bash
cd "$ROBODOJO_ROOT/XPolicyLab"
bash scripts/RoboDojo/download_robodojo_data.sh demo
# 完整数据按需要选择：hdf5、lerobot_v3.0、lerobot_v2.1 或 real。
```

这些训练数据不是运行仿真评测的前置条件；在线评测必需的是 `Assets/` 和实际策略
模型权重。

## 6. 启动与验证

先用零动作 executor 验证协议。它不会取得成功分数：

```bash
python -m robotenon_robodojo_office.server \
  --xpolicylab-root "$ROBODOJO_ROOT/XPolicyLab" \
  --executor-factory robotenon_robodojo_office.executors:create_zero_executor \
  --host 0.0.0.0 --port 19000
```

另一终端启动一个官方 case：

```bash
export ROBODOJO_ROOT=/data/RoboDojo
Tenon run \
  --config extensions/benchmarks/robodojo-office/configs/robotenon_robodojo_office.json \
  --output-dir data/robodojo-office
```

配置中的 `robodojo_root` 为 `null` 时读取 `ROBODOJO_ROOT`。任务名、策略端口、
action type 和 eval count 均在
`configs/robotenon_robodojo_office_task_adapters.json` 显式配置。

## 准确性约束

- 正式分数以 RoboDojo native result 为准，RoboTenon 只做无损附加投影。
- 一个 TaskCase 使用唯一 `ROBODOJO_RUN_ID`；缺失本次结果时直接失败，不回退旧文件。
- 不修改官方 seed、layout、success、score、视频或重启逻辑。
- `RoboTenon` shim 的 `eval_batch` 默认关闭；启用前必须实现并验证 batch executor。
- 当前 Xiaomi RoboCasa365 12D checkpoint 不能自动转换为 RoboDojo dual-X5 策略。
  动作头、相机和坐标系必须由实际 executor 明确适配。

可选的 `--scheduling-strategy static` 按稳定的 case 索引轮流分配固定 Worker。默认的 `global_queue` 会立即为任意空闲 Worker 分配任务，以缓解负载不均。
