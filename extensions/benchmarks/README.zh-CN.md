# Benchmark 评测

[English](README.md) · [**简体中文**](README.zh-CN.md)

用同一个命令评测不同仿真环境，模型与环境分别安装、独立配置。用户实现一个 `vla.py`，通过路径指定模型；无需修改 Graph 或框架注册表。

框架文档中的 [Benchmark 使用指南](../../docs/benchmarks.zh-CN.md) 提供安装、评测、并行和结果的一条完整路线。

## 从哪里开始

| 你要做什么 | 阅读文档 |
| --- | --- |
| 第一次运行 | [快速开始](QUICKSTART.zh-CN.md) |
| 安装 Python 环境、准备公共依赖 | [安装说明](SETUP.zh-CN.md) |
| 下载某个 benchmark 的资产、选择任务 | 下方 benchmark 索引 |
| 评测 HLP + VLA + verifier 完整系统 | [系统接入手册](SYSTEMS.zh-CN.md) |
| 接入自己的模型 | [模型接口](models/README.zh-CN.md) |
| 配置多 GPU、动态 batch、调度与超时 | [完整评测手册](EVALUATION.zh-CN.md) |
| 修改 runtime 或查看验证证据 | [开发者说明](benchmark-support/README.zh-CN.md) |

推荐顺序：**选择环境 → 安装和下载资产 → fake smoke → 真模型 smoke → 正式评测**。

## 三个需要选择的参数

- **benchmark**：仿真环境，例如 `libero_plus`。
- **profile**：任务集与评测协议，例如 `all-0`；可以是内置名称或外部 JSON。
- **backend**：模型 `vla.py` 路径；模型必须支持所选环境的观测和动作空间。

模型 checkpoint 不决定任务协议；更换模型不需要更换评测入口。

<a id="installation-and-assets"></a>

## Benchmark 索引

每个 README 说明配置选择，SETUP 说明仿真依赖、资产下载和本机路径。默认 profile 只是起点，不一定是完整官方协议。

| Benchmark ID | 协议与使用 | 安装与资产 | 默认 profile |
| --- | --- | --- | --- |
| `behavior1k` | [BEHAVIOR-1K](behavior1k/README.zh-CN.md) | [安装说明](behavior1k/SETUP.zh-CN.md) | `eval` |
| `calvin` | [CALVIN](calvin/README.zh-CN.md) | [安装说明](calvin/SETUP.zh-CN.md) | `eval` |
| `duobench` | [DuoBench](duobench/README.zh-CN.md) | [安装说明](duobench/SETUP.zh-CN.md) | `smoke_test` |
| `kinetix` | [Kinetix](kinetix/README.zh-CN.md) | [安装说明](kinetix/SETUP.zh-CN.md) | `eval` |
| `libero` | [LIBERO](libero/README.zh-CN.md) | [安装说明](libero/SETUP.zh-CN.md) | `smoke_test` |
| `libero_mem` | [LIBERO-Mem](libero_mem/README.zh-CN.md) | [安装说明](libero_mem/SETUP.zh-CN.md) | `eval` |
| `libero_plus` | [LIBERO-Plus](libero_plus/README.zh-CN.md) | [安装说明](libero_plus/SETUP.zh-CN.md) | `all-0` |
| `libero_pro` | [LIBERO-Pro](libero_pro/README.zh-CN.md) | [安装说明](libero_pro/SETUP.zh-CN.md) | `eval` |
| `maniskill2` | [ManiSkill2](maniskill2/README.zh-CN.md) | [安装说明](maniskill2/SETUP.zh-CN.md) | `eval` |
| `mikasa` | [MIKASA](mikasa/README.zh-CN.md) | [安装说明](mikasa/SETUP.zh-CN.md) | `eval` |
| `molmospaces` | [MolmoSpaces](molmospaces/README.zh-CN.md) | [安装说明](molmospaces/SETUP.zh-CN.md) | `pick_and_place` |
| `rlbench` | [RLBench](rlbench/README.zh-CN.md) | [安装说明](rlbench/SETUP.zh-CN.md) | `eval` |
| `robocasa` | [RoboCasa 0.2](robocasa/README.zh-CN.md) | [安装说明](robocasa/SETUP.zh-CN.md) | `smoke` |
| `robocasa365` | [RoboCasa365](robocasa365/README.zh-CN.md) | [安装说明](robocasa365/SETUP.zh-CN.md) | `smoke` |
| `robocerebra` | [RoboCerebra](robocerebra/README.zh-CN.md) | [安装说明](robocerebra/SETUP.zh-CN.md) | `eval` |
| `robodojo` | [RoboDojo](robodojo/README.zh-CN.md) | [安装说明](robodojo/SETUP.zh-CN.md) | `smoke_test` |
| `robomme` | [RoboMME](robomme/README.zh-CN.md) | [安装说明](robomme/SETUP.zh-CN.md) | `counting` |
| `robotwin` | [RoboTwin](robotwin/README.zh-CN.md) | [安装说明](robotwin/SETUP.zh-CN.md) | `eval` |
| `simpler` | [SimplerEnv](simpler/README.zh-CN.md) | [安装说明](simpler/SETUP.zh-CN.md) | `google_robot_drawer_va-0` |
| `vlabench` | [VLABench](vlabench/README.zh-CN.md) | [安装说明](vlabench/SETUP.zh-CN.md) | `eval` |

BEHAVIOR-1K 与 RoboDojo 需要 RTX/Isaac 运行条件，尚未完成真实渲染验证；其余环境的已有验证范围见各自 SETUP。fake 测试和选定任务探针不代表全套准确率验证。

## 文件位置

```text
extensions/benchmarks/
  benchmark-support/    统一命令、模型加载、Graph 与调度
  models/               模型后端；也可以放在仓库外
  <benchmark>/
    README.md           profile 选择与环境特有说明
    SETUP.md            安装、资产与首次验证
    configs/            评测协议 JSON
    src/                仿真适配代码
```

`robodojo-office` 和 `robocasa-hlp` 是专用旧集成，使用各自 README 的命令，不属于原生统一入口。

## 同卡多个模型副本

参见 [虚拟 GPU 配置与验证](VIRTUAL_GPUS.zh-CN.md)，支持通用 benchmark 和系统 Graph。
