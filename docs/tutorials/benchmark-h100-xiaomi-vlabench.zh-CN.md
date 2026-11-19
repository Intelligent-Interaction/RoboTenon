# Xiaomi-Robotics-1 + VLABench：复现指南与实测参考

[English](benchmark-h100-xiaomi-vlabench.md) · [**简体中文**](benchmark-h100-xiaomi-vlabench.zh-CN.md)

这篇文档面向准备运行 VLABench 的读者，给出可复现命令、最终结果、速度、任务级明细和排错
建议。示例路径可替换，不公开服务器、账号、私有目录、运行 ID 或文件哈希。

使用 RoboTenon 统一评测入口，Xiaomi-Robotics-1 在 VLABench 五个 Track 上的最终得分为 **60.84%（任务宏平均成功率）**。8 张 H100、160 个仿真 Worker 完成全部 2,460 个 episode，总耗时 **1 小时 53 分 59 秒（约 114 分钟）**，端到端平均吞吐为 **21.58 episodes/分钟**。

本文介绍硬件与模型配置、完整任务结果、并行效率和运行方法。另见 [RoboCasa365 实测报告](benchmark-h100-xiaomi.zh-CN.md)与 [VLABench 资产下载指南](../../extensions/benchmarks/vlabench/README.zh-CN.md)。

## 最终结果

| 指标 | 结果 |
| --- | ---: |
| 最终得分：任务宏平均成功率 | **60.84%** |
| Episode 加权成功率 | **61.18%** |
| 完成数量 | 2,460 / 2,460 |
| 成功 / 未成功 | 1,505 / 955 |
| 运行错误 / 取消 | 0 / 0 |
| 总耗时 | **6,838.54 秒，约 1 小时 54 分钟** |
| 端到端平均速度 | **21.58 episodes/分钟** |

耗时包含模型服务启动、仿真执行和服务关闭，不包含依赖安装与资产下载。以下得分来自一次完整评测。

## 协议与统计口径

评测使用 Track 1、2、3、4、6，各 10 个任务条目，机器人为 `franka`。每个任务最多评测 50 个 episode；所用资产版本中 Track 2 的 `insert_flower` 仅有 10 个配置，因此完整测试集为 **49 × 50 + 10 = 2,460**。环境版本与安装方式见 [SETUP](../../extensions/benchmarks/vlabench/SETUP.zh-CN.md)。

未设置统一 `max_steps`，使用原生任务 horizon：100 步共 500 项、200 步共 1,710 项、300 步共 250 项。这里的步数是适配器的环境动作步数，不是底层 MuJoCo 物理积分步数，也不是模型请求数。

- Episode 加权成功率：`1505 / 2460 = 61.1789%`。
- 任务宏平均：先计算每个 Track/task 的成功率，再对 50 项等权平均，得到 `60.84%`。
- 955 个未成功 episode 均达到任务步数上限；没有运行错误或取消。

由于各任务的 episode 数量不完全相同，宏平均与 episode 加权成功率不同。比较其他结果时，应同时核对任务配置数量、horizon 和统计口径。

## 硬件、环境与全部运行参数

| 项目 | 本次配置 |
| --- | --- |
| GPU | 8 × H100 80GB；每卡 1 个常驻模型副本 |
| CPU | 2 × Intel Xeon Platinum 8558，96 物理核 / 192 逻辑 CPU |
| 节点内存 | 约 2 TiB；这是节点容量，不是本次峰值用量 |
| 模型环境 | Python 3.12，torch 2.7.1+cu128，transformers 5.11.0 |
| 仿真环境 | 独立 VLABench 环境；MuJoCo 3.2.2，dm_control 1.0.22 |
| 模型 | `XiaomiRobotics/Xiaomi-Robotics-1-VLABench` |
| 推理 | BF16（包含整模型转换）、Flash Attention 2 |
| Worker | 20 / GPU，总计 160 |
| Batch | 上限 16，等待窗口 200 ms，服务 pending 上限 256 |
| 调度 | `global_queue`，shuffle 开启，scheduler seed 7 |
| 启动并发 / 回收 | `startup_parallelism=64`；`recycle_after_tasks=1000` |
| 重试 | `max_retries=1`，不代表重试普通任务失败 |
| 超时 | task 7200 s；inference 600 s；model/worker startup 各 1800 s |
| Trace / 历史缓存 | `summary`；history-cache `auto`，实际关闭 |
| Context 上限 | history 64；outputs 64 |
| 动作超时 | 最终派生 `action_timeout_ms=7260000` |
| 视频 / 逐步 trace | `save_videos=false`；`save_traces=false` |
| 渲染 | EGL；环境图像 256 × 256；后端图像处理尺寸参数 480 |
| 记录附加选项 | video_stride 1；video_fps 20（未录像）；memory_mode disabled |
| 额外服务 / 系统配置 | 无；无任务子集或 episode 数覆盖；无 virtual GPU 映射 |

模型后端配置如下，将 `model_path` 替换为实际 checkpoint 路径：

```json
{
  "model_path": "/data/models/Xiaomi-Robotics-1-VLABench",
  "rng_mode": "official_stream",
  "inference_seed": 42,
  "vlabench_image_size": 480,
  "vlabench_cot": false,
  "vlabench_position_offset": [0.0, -0.4, 0.78],
  "vlabench_gripper_threshold": 0.2
}
```

后端 history 为 `length=1, interval=1`，每 5 个动作重新推理。checkpoint 的原始 action chunk 为 10 × 60，执行前 7 维；robot key 为 `vlabench_choice`。适配器将动作增量累加为每个计划内的末端目标，并对角度归一化。模型推理 seed 为 42，调度 seed 为 7；环境使用 Track 中的 episode 配置，不额外指定环境 seed。

线程和运行环境：

```bash
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 OMP_THREAD_LIMIT=1 TBB_NUM_THREADS=1
export OPENCV_FOR_THREADS_NUM=1
export MALLOC_ARENA_MAX=1 MALLOC_TRIM_THRESHOLD_=131072
export MUJOCO_GL=egl PYOPENGL_PLATFORM=egl
export ROBOTENON_LOCAL_RUNTIME_ROOT=/tmp/robotenon-benchmark
ulimit -n 1048576
```

模型进程的 `PYTHONPATH` 包含 VLABench 源码根目录，以便枚举配置；仿真解释器包装脚本清除 `PYTHONPATH`，从独立环境加载依赖。线程变量限制相应库的线程池，并不保证每个进程仅有一个线程。

## 速度与合批结果

| 指标 | 最终值 | 口径 |
| --- | ---: | --- |
| 完成 / 成功 / 失败 | 2460 / 1505 / 955 | 最终结果 |
| 错误 / 取消 | 0 / 0 | 最终结果 |
| 完整耗时 | 6,838.54 s | 包括服务启动、执行和关闭，不含下载与安装 |
| 端到端平均速度 | 21.58 episodes/min | `2460 × 60 / wall_sec` |
| 模型请求数 | 72,229 | 8 个模型服务之和 |
| Batch 调用数 | 47,599 | 8 个模型服务之和 |
| 实际平均 batch | 1.52 | 请求数 / 调用数 |

| 实际 batch size | 调用次数 |
| --- | ---: |
| 1 | 29,373 |
| 2 | 13,222 |
| 3 | 3,884 |
| 4 | 892 |
| 5 | 185 |
| 6 | 35 |
| 7 | 7 |
| 8 | 1 |
| 9–16 | 0 |

160 个仿真 Worker 与 8 个常驻模型副本并行工作，空闲 Worker 从全局队列领取 episode。动态合批将 72,229 个请求合并为 47,599 次模型调用。实际平均 batch 为 1.52，受仿真推进和请求到达节奏影响，没有达到配置上限 16。这是并行配置的实测吞吐；与串行执行的加速比需要单独对照测量。

## 全部任务结果

| Track | 成功 / 总数 | Episode 加权 SR | 任务宏平均 SR |
| --- | ---: | ---: | ---: |
| 1：in distribution | 375 / 500 | 75.00% | 75.00% |
| 2：cross category | 268 / 460 | 58.26% | 56.80% |
| 3：common sense | 252 / 500 | 50.40% | 50.40% |
| 4：semantic instruction | 270 / 500 | 54.00% | 54.00% |
| 6：unseen texture | 340 / 500 | 68.00% | 68.00% |
| 总计 | 1505 / 2460 | 61.18% | 60.84% |

下表单元格为“成功数 / episode 数”；成功率即两数相除。Track 3 的 poker 和 mahjong 使用表内注明的任务名，其余各 Track 使用相同基础任务名。

| 任务 | Track 1 | Track 2 | Track 3 | Track 4 | Track 6 |
| --- | ---: | ---: | ---: | ---: | ---: |
| add_condiment | 27/50 | 14/50 | 13/50 | 14/50 | 28/50 |
| insert_flower | 26/50 | 4/10 | 22/50 | 20/50 | 17/50 |
| select_book | 37/50 | 22/50 | 29/50 | 42/50 | 36/50 |
| select_chemistry_tube | 44/50 | 25/50 | 49/50 | 41/50 | 46/50 |
| select_drink | 37/50 | 22/50 | 24/50 | 22/50 | 34/50 |
| select_fruit | 35/50 | 40/50 | 31/50 | 27/50 | 32/50 |
| select_mahjong（Track 3: select_unique_type_mahjong） | 46/50 | 40/50 | 7/50 | 32/50 | 33/50 |
| select_painting | 35/50 | 27/50 | 29/50 | 32/50 | 35/50 |
| select_poker（Track 3: select_nth_largest_poker） | 50/50 | 42/50 | 15/50 | 13/50 | 50/50 |
| select_toy | 38/50 | 32/50 | 33/50 | 27/50 | 29/50 |

## 运行完整评测

先按 [SETUP](../../extensions/benchmarks/vlabench/SETUP.zh-CN.md) 准备独立的模型和仿真环境，下载资产与 checkpoint，并应用其中的 Open3D 惰性加载补丁。该补丁减少未使用的点云依赖加载开销。

使用仓库内的 [full profile](../../extensions/benchmarks/vlabench/configs/full.json)，将上面的模型 JSON 保存为 `local-configs/xiaomi-vlabench.json`。设置 `MODEL_PY`、`SIM_PY` 为两个环境的解释器，应用前面的线程与渲染设置后，在仓库根目录执行：

```bash
"$MODEL_PY" -m robotenon_benchmark_support.evaluate \
  --benchmark vlabench --profile full \
  --backend ./extensions/benchmarks/models/xiaomi/vla.py \
  --backend-config ./local-configs/xiaomi-vlabench.json \
  --model-python "$MODEL_PY" --sim-python "$SIM_PY" \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-device 20 --batch-size 16 --wait-ms 200 \
  --scheduling-strategy global_queue --scheduler-seed 7 \
  --startup-parallelism 64 --recycle-after-tasks 1000 --max-retries 1 \
  --task-timeout 7200 --inference-timeout 600 \
  --model-startup-timeout 1800 --worker-startup-timeout 1800 \
  --trace-mode summary --history-cache auto \
  --context-history-limit 64 --context-outputs-limit 64 \
  --output runs/xiaomi-vlabench --run-id full-local
```

首次部署建议先用单 GPU、单 Worker，增加 `--max-tasks 1 --episodes-per-task 1` 检查模型输入、动作和渲染。正式评测移除这两个子集参数，并使用独立输出目录。

## 结果读取与适用范围

运行中查看 `status.json` 的 `summary`，结束后查看 `summary.json`；`protocol.json` 保存实际配置。模型 batch 指标保存在节点本地的 `$ROBOTENON_LOCAL_RUNTIME_ROOT/<run-id>/batch-metrics`，应在释放节点前归档。详细查询方式见 [评测手册](../../extensions/benchmarks/EVALUATION.zh-CN.md)。

本结果来自一次完整测试，未测量重复运行方差。动态 batch 和 `official_stream` 随机流可能随调度改变动作轨迹。模型环境使用 Transformers 5.11.0，与 Xiaomi 评测指南中的 4.57.1 不同；安装依赖、资产版本或执行配置变化后需重新测量。本文报告任务成功率，不包含 intention score 和 progress score。

## 常见错误和踩坑点

- **只报告 episode 成功率**：VLABench 各任务 episode 数不完全相同。本文最终得分采用 50 个
  Track/task 的宏平均 60.84%，并同时给出 episode 加权值 61.18%。比较结果前先统一口径。
- **把所有任务 horizon 改成同一个值**：官方任务原生上限包含 100、200 和 300 步。统一改大
  会改变分数和耗时，不能再称为同一协议。
- **把 horizon 失败当成程序错误**：本轮 955 个未成功 episode 都正常达到任务步数上限，
  error / cancelled 均为 0。排错时先区分任务失败与基础设施失败。
- **资产只下载了一部分**：任务配置、mesh、texture 和机器人资源缺一不可。smoke 通过一个任务
  不代表五个 Track 的全部资产已经齐全；全量前应枚举计划并验证各资源入口。
- **每个 suite / Track 分开启动模型**：会重复加载权重并在切换阶段浪费 GPU。框架支持合并计划
  时优先统一 shuffle、全局调度并最后 reduce。
- **Worker 过多导致 CPU 和内存争用**：本轮使用 160 个 Worker，不代表越多越快。限制数值库
  线程，并以稳定 episodes/min、RSS 与 reset 时延决定并发量。
- **batch 配置值被误当成实际值**：请求由仿真异步到达，必须读取实际 batch 直方图。增大上限
  或等待窗口可能增加动作延迟，不保证吞吐提高。
- **仿真 GPU 渲染与模型 CUDA 混为一谈**：模型有 GPU 利用率不能证明 renderer 正常使用 EGL。
  分别检查模型服务、仿真进程和设备映射。
- **忽略依赖版本差异**：本次模型环境与 Xiaomi 指南版本不同，这是复现限制而不是可以隐藏的
  细节。更新 Transformers、NumPy 或仿真依赖后应重新 smoke 并重新测量。
- **全量开启 recorder**：录像适合定位失败，但会增加编码和磁盘开销。本文耗时对应关闭录像的
  配置，开启后不能直接比较速度。
