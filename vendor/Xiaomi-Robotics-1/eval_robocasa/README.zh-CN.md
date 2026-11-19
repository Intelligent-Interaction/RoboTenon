# RoboCasa 评测

[English](README.md) · [**简体中文**](README.zh-CN.md)

本指南介绍如何在 [RoboCasa](https://robocasa.ai/) 上评测 **Xiaomi-Robotics-1**，包括安装仿真环境、启动推理服务、运行评测客户端与查看结果。

> **架构。** 服务端加载模型并通过套接字返回动作。客户端运行 RoboCasa 仿真器，使用 Hugging Face `AutoProcessor` 构建输入并解码动作。两端分别运行在**独立的 Conda 环境**中。

---

## 前置条件

### 1. 部署环境（`mibot`）

服务端在此环境运行。按 **[部署指南](../docs/DEPLOYMENT.zh-CN.md)** 完成一次安装即可。

### 2. RoboCasa 仿真环境（`robocasa`）

RoboCasa 评测使用 **v0.2**、Python 3.10 和固定版本的 PyTorch。v1.0 用于 RoboCasa365。

```bash
conda create -n robocasa python=3.10 -y
conda activate robocasa

# robosuite (master) + RoboCasa (v0.2)
git clone https://github.com/ARISE-Initiative/robosuite
cd robosuite && pip install -e . && cd ..
git clone https://github.com/robocasa/robocasa --branch v0.2
cd robocasa && pip install -e . && cd ..

# If numpy/numba install fails, try conda instead:
# conda install -c numba numba=0.56.4 -y

# XR-1 evaluation client dependencies (torch/numpy/mujoco come with robocasa).
# transformers must be exactly 4.57.1; other versions are unverified.
pip install transformers==4.57.1 imageio[ffmpeg] scipy tyro tqdm

# Set up RoboCasa system variables and download kitchen assets (~10 GB)
python -m robocasa.scripts.setup_macros
python -m robocasa.scripts.download_kitchen_assets
```

### 3. 模型权重

从 [RoboCasa 模型页面](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa)下载微调权重。服务端与客户端都必须能读取同一个模型目录。

```bash
export MODEL_PATH="$PWD/checkpoints/Xiaomi-Robotics-1-RoboCasa"
hf download XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa --local-dir "$MODEL_PATH"
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

在另一个终端先激活 **RoboCasa** 环境。启动器不会自动切换环境。

```bash
conda activate robocasa
bash scripts/launch_robocasa.sh 8 ./eval_robocasa/eval_logs "$MODEL_PATH"
```

| 参数 | 含义 |
| :--- | :--- |
| `8` | 客户端 Worker 数，必须与服务端实例数一致 |
| `./eval_robocasa/eval_logs` | 日志与结果的输出目录 |
| `"$MODEL_PATH"` | 模型权重目录，客户端从中加载 processor |

每个 Worker 独立记录成功率与 rollout 视频，启动器在全部任务结束后合并逐任务结果。

---

## 评测结果

参考运行覆盖 24 个任务，每个任务 100 个 episode。

| 任务 | 成功率 | 任务 | 成功率 |
| :--- | :---: | :--- | :---: |
| PnPCounterToCab | 56% | OpenDrawer | 77% |
| PnPCabToCounter | 64% | CloseDrawer | 96% |
| PnPCounterToSink | 59% | TurnOnStove | 82% |
| PnPSinkToCounter | 71% | TurnOffStove | 29% |
| PnPCounterToMicrowave | 30% | TurnOnSinkFaucet | 91% |
| PnPMicrowaveToCounter | 37% | TurnOffSinkFaucet | 97% |
| PnPCounterToStove | 73% | TurnSinkSpout | 86% |
| PnPStoveToCounter | 62% | CoffeeSetupMug | 42% |
| OpenSingleDoor | 87% | CoffeeServeMug | 77% |
| CloseSingleDoor | 100% | CoffeePressButton | 82% |
| OpenDoubleDoor | 99% | TurnOnMicrowave | 91% |
| CloseDoubleDoor | 94% | TurnOffMicrowave | 99% |
| **平均** | **74.2%** | | |

不同 GPU 机器上的结果可能有所差异。上游在 `eval_logs/` 提供逐 rank 结果与最终汇总，以便对比和排查问题。
