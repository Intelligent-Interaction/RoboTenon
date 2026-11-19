# OpenVLA-OFT / LIBERO-Spatial

[English](README.md) · [**简体中文**](README.zh-CN.md)

本目录接入原版 LIBERO-Spatial，使用官方 Spatial checkpoint，仅接受单请求：`supports_batch=False`，`batch-size` 必须为1。已完成固定协议下的500个episode，成功491个（98.20%）；验证范围见下文，不代表官方四个suite平均成绩复现。

## 文件与调用关系

| 文件 | 职责 |
| --- | --- |
| `vla.py` | 轻量 SPEC、观测参数和 create；读取声明不导入 Torch/TF |
| `implementation.py` | 会话管理、输入检查、官方推理调用、原生动作转换 |
| `configs/libero-spatial.json` | 可移植的路径示例与模型 seed；使用前复制并填写实际路径 |
| `SOURCE.json` | 固定官方代码、依赖 fork 和目标权重 revision |
| `../../libero/configs/openvla_oft_spatial.json` | 对应任务/环境接口和协议配置 |

`evaluate → BackendRuntime → VLAProvider → OpenVLAOFT.act_batch`；输出通过 `BenchmarkPolicy → Body → LIBEROBenchmark.step` 执行。8步动作的排队和成功后丢弃由框架负责，adapter不另建环境循环。

## 输入与动作约定

- `open_session(session_id, task_info)` 只接受 `benchmark_id=libero_benchmark`、`task.suite=libero_spatial`。Spatial权重不能直接当成其他suite权重。
- `act_batch` 每次一个请求，返回相同 `request_id`。
- `observation.history` 长度1；其中 `images.agentview` 和 `images.wrist` 均为256×256 uint8 RGB。环境已翻转两轴，adapter不再翻转。
- `states` 为 `[eef_pos3, axisangle3, gripper_qpos2]`。复制后交给官方代码做checkpoint统计归一化，避免修改框架缓存。
- 官方 `get_vla_action` 负责JPEG encode/decode、Lanczos3 resize到224、中心裁剪、prompt、小写任务文本、processor和预测。没有改成PIL近似预处理。
- 返回8×7 action chunk：前6维直接使用；夹爪执行 `-sign(2*g-1)`，与官方 normalize+binarize+invert一致。g=0.5会返回0，所以专用profile设置 `discretize_gripper=false`。
- profile显式保留seed=7、env_seed=0、10步等待、delta控制、任务原始220步动作预算。初始化等待在动作预算之外；初始状态按episode索引选取。
- 当前框架的 `quat_no_antipodal` 路径会将轴角转成float32；官方独立脚本可能保留更高精度。已有运行保留此实现，不能宣称跨框架输入或轨迹逐位一致。

## 验证结果

2026-10-18完成一次LIBERO-Spatial完整运行：10个任务，每任务使用预定义初始状态0–49，共500个episode；成功491个（98.20%），9个达到原生horizon后未成功，技术错误和未完成均为0。

使用 `SOURCE.json` 中固定的Spatial权重与官方源码，seed=7、env_seed=0、10步初始化等待、220步原生动作预算、每次8步动作、单张A100、单Worker、batch=1、无自动重试。实际模型为BF16、SDPA；真实动作预测6,684次，执行动作51,703步。

内部验收核对了样本/会话唯一性、原始动作返回、processor输入、请求与观察对应、执行动作及夹爪转换、文件哈希、终止观察、原生录像和Trace。完整原始数据与验收材料保留在内部运行包中，不随模型后端提交。该历史运行同时使用了单独的录像、终止观察与Trace记录修复；这些共享记录修复及内部证据采集脚本不属于本次模型适配提交。下方通用命令不能视为相同的记录验收环境，adapter本身不会自动导出processor张量或原始预测动作副本。

这是固定权重和RoboTenon执行器上的单种子Spatial结果。Object、Goal、LIBERO-10及其他benchmark尚未接入此adapter；没有开展多种子统计、batch性能评测或官方独立evaluator配对复现。

## 环境准备（安装本身不执行模型）

模型环境使用Python3.10，与仿真环境分开安装。以下均为示例路径，使用前替换为实际目录；不需要训练示范数据。

```bash
git clone https://github.com/moojink/openvla-oft.git /opt/src/openvla-oft
git -C /opt/src/openvla-oft checkout e4287e94541f459edc4feabc4e181f537cd569a8

conda create -p /opt/envs/openvla-oft python=3.10 -y
MODEL_PY=/opt/envs/openvla-oft/bin/python
ROBOTENON=/path/to/RoboTenon

# 国内HF访问使用镜像；推理阶段仍要求完整本地权重。
export HF_ENDPOINT=https://hf-mirror.com
"$MODEL_PY" -m pip install -r "$ROBOTENON/extensions/benchmarks/models/openvla-oft/requirements.txt"
"$MODEL_PY" -m pip install --no-deps -e /opt/src/openvla-oft
"$MODEL_PY" -m pip install -e "$ROBOTENON" -e "$ROBOTENON/extensions/benchmarks/benchmark-support"
```

`requirements.txt` 固定Torch2.2、两个Git fork，以及已验证环境中的 `tensorflow-metadata==1.15.0`、`wandb==0.16.6` 兼容约束；其余范围依赖不是完整lock，新环境仍需核验安装与导入。必须使用官方Transformers fork，其双向注意力支持不能用普通Transformers替代。当前使用SDPA，不要求另装FlashAttention。

LIBERO仿真环境另外按 [SETUP](../../libero/SETUP.zh-CN.md) 准备，`SIM_PY` 使用实际路径，在其中安装框架、benchmark-support、libero extension。该SETUP与官方OFT脚本的robosuite依赖版本不同；本次适配不改变已有benchmark依赖，也不宣称跨版本严格复现。保留源版本和环境清单，后续比较前单独核对。

按 `SOURCE.json` 中的checkpoint revision预先下载并核对完整权重，配置使用本地绝对路径。adapter只读加载本地模型与action/proprio head，不调用会改写checkpoint的官方 `get_vla()`。

## CPU接口核验

在有 NumPy、Pillow、pytest 和 RoboTenon 核心依赖的环境执行：

```bash
cd /path/to/RoboTenon
CUDA_VISIBLE_DEVICES="" python -m pytest -q \
  extensions/benchmarks/benchmark-support/tests/test_openvla_oft_adapter.py
```

检查仅覆盖声明加载、profile设置、观测与状态、动作转换、session/request对应和错误不重试；使用模拟预测，不加载Torch/TF、checkpoint或仿真器。通过不等于真实模型调用通过。

## 运行示例

修改backend JSON副本中的路径后，下面命令表示 **1个任务 × 1个episode、单Worker、batch1、最多220步动作、无自动重试**；不是一次模型调用。若完整执行预算，最多28次动作预测。成功可能提前结束。

```bash
cd /path/to/RoboTenon
MODEL_PY=/opt/envs/openvla-oft/bin/python
SIM_PY=/opt/envs/libero/bin/python
# 复制后填写实际的model_path和upstream_path，不修改公共示例配置。
mkdir -p /data/configs
cp ./extensions/benchmarks/models/openvla-oft/configs/libero-spatial.json \
  /data/configs/openvla-oft-spatial.json
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero --profile openvla_oft_spatial \
  --backend ./extensions/benchmarks/models/openvla-oft/vla.py \
  --backend-config /data/configs/openvla-oft-spatial.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 --max-retries 0 --no-shuffle \
  --output "runs/openvla-oft-spatial-$(date +%Y%m%d-%H%M%S)"
```

首次部署先使用上述单episode范围检查模型加载、两路图像、状态、动作和终止行为。专用profile本身为10个任务×50个episode；需要完整Spatial评测时，去掉 `--max-tasks 1 --episodes-per-task 1` 并使用新的输出目录。不要把单episode检查当作完整评测结果。

## 官方来源

- [代码与依赖](https://github.com/moojink/openvla-oft/tree/e4287e94541f459edc4feabc4e181f537cd569a8)
- [LIBERO评测](https://github.com/moojink/openvla-oft/blob/e4287e94541f459edc4feabc4e181f537cd569a8/LIBERO.md)
- [原生输入与动作流程](https://github.com/moojink/openvla-oft/blob/e4287e94541f459edc4feabc4e181f537cd569a8/experiments/robot/libero/run_libero_eval.py)

官方97.1%是四个suite独立checkpoint的平均；这里仅实现Spatial，不能使用该数字描述此adapter的结果。
