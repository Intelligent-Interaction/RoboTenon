# VLABench 评测

[English](README.md) · [**简体中文**](README.zh-CN.md)

本指南介绍如何在 [VLABench](https://github.com/OpenMOSS/VLABench) 上评测 **Xiaomi-Robotics-1**，包括安装仿真环境、启动推理服务、运行评测客户端与查看结果。

> **架构。** 服务端加载模型并通过套接字返回动作。客户端运行 VLABench 仿真器，使用 Hugging Face `AutoProcessor` 构建输入并解码动作。两端分别运行在**独立的 Conda 环境**中。

---

## 前置条件

### 1. 部署环境（`mibot`）

服务端在此环境运行。按 **[部署指南](../docs/DEPLOYMENT.zh-CN.md)** 完成一次安装即可。

### 2. VLABench 仿真环境（`vlabench`）

```bash
conda create -n vlabench python=3.10 -y
conda activate vlabench

# VLABench — installed under the current directory (repository root)
export VLABENCH_ROOT="$PWD/VLABench"
git clone https://github.com/OpenMOSS/VLABench.git "$VLABENCH_ROOT"
cd "$VLABENCH_ROOT"
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
python scripts/download_assets.py

# XR-1 evaluation client dependencies.
# transformers must be exactly 4.57.1; other versions are unverified.
python -m pip install \
  torch==2.8.0 torchvision==0.23.0 \
  --index-url https://download.pytorch.org/whl/cu128
python -m pip install transformers==4.57.1 tyro tqdm pillow openai

# Pinned MuJoCo / dm_control
python -m pip install --force-reinstall --no-deps \
  mujoco==3.2.2 mujoco-mjx==3.2.2 dm_control==1.0.22

# RRT dependency (pinned commit)
mkdir -p "$VLABENCH_ROOT/third_party"
cd "$VLABENCH_ROOT/third_party"
git clone https://github.com/motion-planning/rrt-algorithms.git
cd rrt-algorithms
git checkout e51d95ee489a225220d6ae2a764c4111f6ba7d85
python -m pip install -e . --no-deps
cd - >/dev/null
```

评测前设置其余运行时变量，使用无 X11 的 EGL 渲染。上文已设置 `VLABENCH_ROOT`。

```bash
export MUJOCO_GL=egl
export PYOPENGL_PLATFORM=egl
export MUJOCO_EGL_DEVICE_ID=0
export TOKENIZERS_PARALLELISM=false
```

评测程序从 `$VLABENCH_ROOT/configs/evaluation/tracks/` 读取官方各赛道配置。无显示器服务器不应导入 `dm_control.viewer`。VLABench 使用 EGL 渲染，无需 X11 显示服务。

### 3. 模型权重

从 [VLABench 模型页面](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-VLABench)下载微调权重。服务端与客户端都必须能读取同一个模型目录。

```bash
export MODEL_PATH="$PWD/checkpoints/Xiaomi-Robotics-1-VLABench"
hf download XiaomiRobotics/Xiaomi-Robotics-1-VLABench --local-dir "$MODEL_PATH"
```

Processor 需要支持 `vlabench_choice` 机器人键。发布模型的原始动作形状为 `[10, 60]`，其中前 7 维可执行。`0:3` 为位置增量，`3:6` 为欧拉角增量，`6` 为夹爪动作。每次返回 10 步动作，每执行 5 步重新规划。

---

## 运行评测

### 第一步：启动推理服务

在终端中激活**部署环境**，然后从仓库根目录运行下列命令。

```bash
conda activate mibot
bash scripts/deploy.sh "$MODEL_PATH" 8 8   # <model_path> <num_ports> <num_gpus>
```

此命令在 tmux 的 `model_servers` 会话中启动八个服务，使用端口 `10086`–`10093`。运行 `tmux attach -t model_servers` 可查看状态。**先启动服务端，再运行客户端。**

### 第二步：运行评测客户端

在另一个终端先激活 **VLABench** 环境。启动器不会自动切换环境。

```bash
conda activate vlabench
bash scripts/launch_vlabench.sh 8 ./eval_vlabench/eval_logs "$MODEL_PATH"
```

| 参数 | 含义 |
| :--- | :--- |
| `8` | 客户端 Worker 数，建议与服务端实例数一致 |
| `./eval_vlabench/eval_logs` | 结果输出目录 |
| `"$MODEL_PATH"` | 模型权重目录，客户端从中加载 processor |

启动器依次评测五条赛道，即 `track_1_in_distribution`、`track_2_cross_category`、`track_3_common_sense`、`track_4_semantic_instruction` 和 `track_6_unseen_texture`。每条赛道的 episode 分配到各 Worker，所有赛道结束后合并结果。

**冒烟测试**使用一个 Worker，每个任务运行一个 episode。

```bash
conda activate vlabench
NUM_EVAL_EPISODES=1 \
    bash scripts/launch_vlabench.sh \
    1 ./eval_vlabench/eval_logs_smoke "$MODEL_PATH"
```

**配置参数。** 可通过环境变量 `BASE_PORT`、`NUM_EVAL_EPISODES`、`ROBOT_TYPE`、`ACTION_CHUNK_SIZE`、`REPLAN_STEPS`、`VISUALIZATION`（1/0）和 `COT`（1/0）覆盖启动器设置。

**仅运行一条赛道或部分任务**时，可直接调用 `eval_vlabench/main.py`。

```bash
python -u eval_vlabench/main.py \
  --model-path "$MODEL_PATH" \
  --eval-track track_1_in_distribution \
  --n-episode 50 \
  --host 127.0.0.1 --port 10086 \
  --robot-type vlabench_choice \
  --state-dim 60 --action-dim 7 \
  --action-chunk-size 10 --replan-steps 5 \
  --visualization --no-cot \
  --save-dir ./eval_vlabench/eval_logs/track_1_in_distribution
```

使用 `--tasks "task_a task_b"` 可选择指定任务子集。

---

## 评测结果

使用 `scripts/launch_vlabench.sh` 时，输出目录结构如下。

```text
<output_directory>/
  track_1_in_distribution/
    dispatch.log
    metrics.json
    dispatch_manifest.json
    <task_name>/
      detail_info.json
      videos/
      worker_logs/
  track_2_cross_category/
  track_3_common_sense/
  track_4_semantic_instruction/
  track_6_unseen_texture/
  merged_results.json
  results.md
```

评测指标包括**成功率（SR）**、**意图得分（IS）**和**进展得分（PS）**。全部赛道结束后，启动器写入 `merged_results.json` 和 `results.md`。每条赛道的得分取各任务的宏平均，总体得分取全部任务条目的宏平均，各 episode 不单独加权。
