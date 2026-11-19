# RoboCasa365 评测

[English](README.md) · [**简体中文**](README.zh-CN.md)

本指南介绍如何在 [RoboCasa365](https://robocasa.ai/) 上评测 **Xiaomi-Robotics-1**，包括安装仿真环境、启动推理服务、运行评测客户端与查看结果。

> **架构。** 服务端加载模型并通过套接字返回动作。客户端运行 RoboCasa365 仿真器，使用 Hugging Face `AutoProcessor` 构建输入并解码动作。两端分别运行在**独立的 Conda 环境**中。

---

## 前置条件

### 1. 部署环境（`mibot`）

服务端在此环境运行。按 **[部署指南](../docs/DEPLOYMENT.zh-CN.md)** 完成一次安装即可。

### 2. RoboCasa365 仿真环境（`robocasa_365`）

先按 [RoboCasa 官方安装指南](https://robocasa.ai/docs/build/html/introduction/installation.html)安装 RoboCasa365，再安装 XR-1 评测客户端的依赖。**transformers 必须使用 `4.57.1`**，其他版本尚未验证。

```bash
pip install transformers==4.57.1 imageio[ffmpeg] tqdm scipy
```

使用无显示器渲染时，安装操作系统提供的 EGL/OpenGL 运行库，并设置 `MUJOCO_GL=egl`。

### 3. 模型权重

从 [RoboCasa365 模型页面](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365)下载微调权重。服务端与客户端都必须能读取同一个模型目录。

```bash
export MODEL_PATH="$PWD/checkpoints/Xiaomi-Robotics-1-RoboCasa365"
hf download XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365 --local-dir "$MODEL_PATH"
```

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

在另一个终端运行客户端。启动器会根据 `CONDA_ENV` 自行激活 **RoboCasa365** 环境，无需提前执行 `conda activate`。

```bash
CONDA_ENV=robocasa_365 \
    bash scripts/launch_robocasa365.sh \
    8 eval_results/robocasa365 "$MODEL_PATH"
```

| 参数 | 含义 |
| :--- | :--- |
| `8` | 客户端 Worker 数，必须与服务端实例数一致 |
| `eval_results/robocasa365` | 结果输出目录 |
| `"$MODEL_PATH"` | 模型权重目录，客户端从中加载 processor |

Worker 默认连接端口 `10086`–`10093`。各 Worker 共享 rollout 队列，完成当前 rollout 后以原子方式领取下一个任务。Rollout 不固定分配给 GPU。Worker 沿用启动器的 CUDA 可见性，并保持 `MUJOCO_EGL_DEVICE_ID` 未设置，与参考运行配置一致。

**冒烟测试**使用单个任务执行一次试验。

```bash
CONDA_ENV=robocasa_365 NUM_TRIALS=1 \
    bash scripts/launch_robocasa365.sh \
    1 eval_results/robocasa365-smoke "$MODEL_PATH" \
    --task-name CloseBlenderLid --horizon 20
```

**配置参数。** `BASE_PORT`、`SERVER_ADDR`、`PYTHON` 和 `CONDA_ENV` 可覆盖机器相关设置。`SPLIT`、`TASK_SET`、`NUM_TRIALS`、`REPLAN_STEPS`、`OBS_HISTORY`、`OBS_INTERVAL`、`SEED` 和 `CROP_RATIO` 可覆盖评测设置。

---

## 评测结果

对于运行 ID `<run-id>`，输出目录结构如下。

```text
eval_results/robocasa365/<run-id>/
  summary.json
  <TaskName>/
    stats.json
    episode_<episode>_seed_<seed>_<success|failure>.mp4

eval_results/robocasa365/scheduler/<run-id>/
  manifest.json  pending/  running/  results/  errors/  logs/
```

最终合并结果前，程序会检查全局 episode 索引是否缺失、重复或不一致。Worker 失败时会留下错误记录，启动器以非零状态退出。

**参考配置。** 使用 `pretrain` 划分和 `target50` 任务集，每任务 50 次试验，观测历史长度 4、采样间隔 2、每次推理执行 16 个动作、环境种子 7、裁剪比例 0.95。2,500 个 episode 中有 1,432 个成功，按 episode 计的成功率为 57.28%。
