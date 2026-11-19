# Xiaomi Robotics 0 + LIBERO：复现指南与实测参考

[English](benchmark-h100-xiaomi-libero.md) · [**简体中文**](benchmark-h100-xiaomi-libero.zh-CN.md)

这篇文档面向准备运行 Xiaomi Robotics 0 的读者：先告诉你能得到什么结果，再给出可移植的
配置、启动命令和排错方法。文中的路径都是示例，不包含评测服务器、账号、内部目录、运行 ID
或文件哈希。

使用的 checkpoint 是
[`XiaomiRobotics/Xiaomi-Robotics-0-LIBERO`](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-0-LIBERO)，
环境是基础 LIBERO 的 `spatial`、`object`、`goal` 和 `10` 四个 suite。安装资产前先读
[LIBERO SETUP](../../extensions/benchmarks/libero/SETUP.zh-CN.md)，模型接口见
[Xiaomi Robotics 0](../../extensions/benchmarks/models/xiaomi-robotics0/README.zh-CN.md)。

## 先看结论

在 8 张 H100、每卡 36 个仿真 Worker 的配置下，四个 suite 共 2,000 个 episode 全部完成：

- 最终成功率：**97.15%（1,943 / 2,000）**；
- 完整耗时：**20 分 35 秒**；
- 端到端吞吐：**97.15 episodes/分钟**；
- 执行错误 / 取消：**0 / 0**；
- 57 个失败 episode 均为运行到原生 horizon 后仍未成功，不是程序崩溃。

这里的“成功率”和“每分钟 episode 数”碰巧都是 97.15，二者含义完全不同。

| Suite | Horizon | 成功 / 总数 | 成功率 |
| --- | ---: | ---: | ---: |
| `libero_spatial` | 220 | 468 / 500 | 93.60% |
| `libero_object` | 280 | 496 / 500 | 99.20% |
| `libero_goal` | 300 | 491 / 500 | 98.20% |
| `libero_10` | 520 | 488 / 500 | 97.60% |
| **合计** | — | **1,943 / 2,000** | **97.15%** |

每个 suite 都是 10 个任务、每任务 50 个 episode，因此总成功率也等于四个 suite 成功率的
简单平均。若修改每任务 episode 数，就需要重新按实际 episode 数加权。

## 怎么理解 57 个失败

最终运行没有模型服务错误、Worker 丢失或取消。57 个失败全部以 `horizon` 结束，含义是策略
在该任务允许的原生步数内没有触发成功条件，而不是 evaluator 超时。

失败高度集中在少数任务：

| 任务 | Suite | 成功 / 50 | 失败数 |
| --- | --- | ---: | ---: |
| pick up the black bowl on the ramekin and place it on the plate | spatial | 21 / 50 | 29 |
| open the top drawer and put the bowl inside | goal | 45 / 50 | 5 |
| put the white mug on the plate and put the chocolate pudding to the right of the plate | libero_10 | 45 / 50 | 5 |
| put both moka pots on the stove | libero_10 | 47 / 50 | 3 |
| put the yellow and white mug in the microwave and close it | libero_10 | 47 / 50 | 3 |

其余失败分散在若干任务中。最弱的 spatial 任务单独贡献 29 个失败，占全部失败的一半以上，
因此只看 suite 平均值会掩盖明显的任务级短板。排查模型行为时应优先回放或录制这个任务，
而不是先统一延长所有任务的 horizon。

## 本次运行配置

| 项目 | 配置 | 读者需要注意什么 |
| --- | --- | --- |
| GPU | 8 × NVIDIA H100 80GB | 每张卡常驻一个模型副本 |
| CPU / 内存 | 96 物理核、192 逻辑 CPU，约 2 TiB 内存 | 这是实测机器，不是最低配置 |
| 仿真 Worker | 每卡 36 个，共 288 个 | 小机器必须从 1–4 个逐步增加 |
| 协议 | 四个 suite 合并，每任务 50 个 episode | 共 40 个任务、2,000 个 episode |
| 调度 | 全局队列、预先 shuffle、调度 seed 7 | 避免按 suite 顺序导致 GPU 空闲和重复加载模型 |
| 模型 batch | 上限 32，等待 100ms | 实际平均 batch 只有 1.45 |
| Worker 回收 | 1,000 个任务后回收 | 本轮等同于不主动频繁回收 |
| 图像 | base + wrist，两路 256×256 RGB | 缺少 wrist 图像会被模型适配器拒绝 |
| 动作 | 连续 7DoF，每 10 步重新规划 | 不能把 10 改成“加速参数”后仍声称同协议 |
| 记录 | 关闭视频和完整 trace | 开启后速度和磁盘占用会变化 |

模型和仿真使用两个独立 Python 环境。模型环境负责 PyTorch、Transformers、checkpoint 和
FlashAttention；仿真环境负责 LIBERO、robosuite、MuJoCo 与渲染。不要为了省事把两套依赖
硬装进同一个环境，NumPy、MuJoCo 和模型依赖冲突会让问题很难定位。

高并发时建议限制数值库线程，避免 288 个进程各自再创建一组 CPU 线程：

```bash
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export TOKENIZERS_PARALLELISM=false
export MALLOC_ARENA_MAX=2
```

## 从 smoke 到全量

先确认模型输入、动作和成功信号，不要第一次就启动 288 个 Worker：

```bash
export MODEL_PY=/opt/envs/xiaomi-libero/bin/python
export SIM_PY=/opt/envs/libero/bin/python

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero --profile smoke_test \
  --backend ./extensions/benchmarks/models/xiaomi-robotics0/vla.py \
  --backend-config ./local-configs/xiaomi-robotics0-libero.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0 --workers-per-device 1 \
  --batch-size 1 --wait-ms 0 \
  --max-tasks 1 --episodes-per-task 1 \
  --output runs/xiaomi-robotics0-libero-smoke
```

检查 `status.json` 中没有 error、episode 能推进、两路图像尺寸正确，再运行完整协议：

```bash
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false MALLOC_ARENA_MAX=2
export EVAL_OUTPUT="runs/xiaomi-robotics0-libero-$(date +%Y%m%d-%H%M%S)"

"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark libero --profiles spatial object goal 10 \
  --backend ./extensions/benchmarks/models/xiaomi-robotics0/vla.py \
  --backend-config ./local-configs/xiaomi-robotics0-libero.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 36 --batch-size 32 --wait-ms 100 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --episodes-per-task 50 --startup-parallelism 8 \
  --max-retries 1 --task-timeout 14400 --inference-timeout 600 \
  --model-startup-timeout 1800 --worker-startup-timeout 1800 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output "$EVAL_OUTPUT"
```

本地模型配置使用自己的 checkpoint 路径，不要把服务器目录提交到仓库：

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-0-LIBERO",
  "device": "cuda",
  "metrics_dir": "/data/runs/xiaomi-robotics0/metrics"
}
```

## 实际 batch 与速度

配置中的 32 只是 batch 上限。本次 30,714 个模型请求实际合并为 21,169 次调用，平均 batch
为 1.45；调用次数减少 31.08%，但这不是端到端加速倍数。

| 实际 batch size | 调用次数 | 占全部调用 |
| ---: | ---: | ---: |
| 1 | 13,555 | 64.03% |
| 2 | 5,997 | 28.33% |
| 3 | 1,338 | 6.32% |
| 4 | 248 | 1.17% |
| 5 | 27 | 0.13% |
| 6 | 4 | 0.02% |
| 7–32 | 0 | 0% |

单次模型调用平均约 175ms。请求到达时间受仿真步长、任务是否提前成功和 reset 时间影响，
所以增加 `batch-size` 不会自动形成大 batch。若 GPU 利用率不高，应同时检查 Worker 数、CPU
争用和实际 batch 直方图，而不是只继续增大 batch 上限。

## 常见错误和踩坑点

### 1. 只运行了一个 suite

`--profile spatial` 只覆盖 500 个 episode，不是完整四 suite 结果。使用
`--profiles spatial object goal 10` 可以一次生成合并计划、统一 shuffle 和汇总，也避免每个
suite 都重新启动模型服务。

### 2. 把 `completed` 当成成功

`completed` 表示 episode 正常结束；任务是否成功必须读取 `task_success`。本次 2,000 个都
completed，但其中 57 个是正常跑到 horizon 的任务失败。

### 3. 通过统一放大 horizon 提高分数

这会改变协议，得到的分数不能再与原生 LIBERO 配置直接比较。先定位失败任务、相机输入、
动作变换和 gripper 语义；只有研究不同控制预算时才另建 profile，并明确标注。

### 4. 模型环境和仿真环境互相污染

不同 LIBERO fork 都导入同名 `libero` 包，模型栈又有自己的 PyTorch / Transformers 约束。
为每个仿真 fork 使用独立环境和独立 LIBERO 配置，确认 `SIM_PY` 实际导入的是预期源码。

### 5. 下载了训练数据，却缺少在线仿真资产

在线评测需要 BDDL、initial states、mesh 和 texture；不需要整套 demonstration HDF5。
只有训练数据并不能解决运行时缺模型或纹理文件的问题，按 SETUP 的资产清单准备即可。

### 6. 直接照抄 36 Worker/卡

该并发量依赖大量 CPU 和内存。Worker 过多会造成 reset 争用、内存峰值甚至 OOM，吞吐反而
下降。新机器从每卡 1、4、8 开始，记录 episodes/min、RSS 和 GPU 利用率后再增加。

### 7. Headless 渲染配置错误

服务器通常需要 `MUJOCO_GL=egl`。如果 Worker 能启动但 GPU 没有渲染负载，检查 EGL 设备映射、
容器驱动可见性以及仿真进程实际使用的 Python；模型进程有 CUDA 不代表仿真渲染已使用 GPU。

### 8. 视频与 trace 改变速度

本次速度是在关闭视频和完整 trace 的条件下测得。排错可以只为少量任务开启录像；全量开启
会增加编码、磁盘写入和 I/O，不能再直接引用本文的 20 分 35 秒。

## 结果边界

- 结果来自一次完整运行，没有用多组调度 seed 重复实验，也没有置信区间。
- 动态 batch、BF16 和多模型副本可能带来微小数值或时序差异，不承诺跨硬件逐 episode 一致。
- 这里报告的是 RoboTenon 当前固定依赖和协议下的实测值；与其他实现比较前，应核对任务集合、
  每任务 episode 数、initial states、horizon、相机和 action chunk。
- 本文不公开评测基础设施身份信息和私有文件布局；复现所需的公共依赖与资产入口均在 SETUP。
