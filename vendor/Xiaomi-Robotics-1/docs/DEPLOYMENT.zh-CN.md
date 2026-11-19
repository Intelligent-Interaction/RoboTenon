# 🚀 部署指南

[English](DEPLOYMENT.md) · [**简体中文**](DEPLOYMENT.zh-CN.md)

本文介绍 **Xiaomi-Robotics-1（XR-1）**的环境安装与推理服务启动方式。

XR-1 基于 Hugging Face Transformers 🤗 部署，用于机器人操作任务。结合 Flash Attention 2 与 bfloat16 精度，模型可在消费级 GPU 上运行。

**transformers** 必须使用 `4.57.1`，其他版本尚未验证。上游团队已验证并推荐 **PyTorch 2.8.0**、torchvision 0.23.0 与 torchaudio 2.8.0 的组合。

---

## 1️⃣ 安装环境

创建部署环境（`mibot`）并安装依赖。

```bash
# Create a Conda environment with Python 3.12
conda create -n mibot python=3.12 -y
conda activate mibot

# Install PyTorch
pip install torch==2.8.0 torchvision==0.23.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
# Install transformers
pip install transformers==4.57.1
# Install flash-attn
pip uninstall -y ninja && pip install ninja
pip install flash-attn==2.8.3 --no-build-isolation
# or pip install https://github.com/Dao-AILab/flash-attention/releases/download/v2.8.3/flash_attn-2.8.3+cu12torch2.8cxx11abiTRUE-cp312-cp312-linux_x86_64.whl

sudo apt-get install -y libegl1 libgl1 libgles2
```

---

## 2️⃣ 启动推理服务

推理采用**客户端–服务端架构**，分别由 `deploy/client.py` 和 `deploy/server.py` 实现。

先克隆仓库并进入项目目录。启动脚本位于 `scripts/`。

```bash
git clone https://github.com/XiaomiRobotics/Xiaomi-Robotics-1
cd Xiaomi-Robotics-1
```

可以直接使用 Hugging Face 模型 ID 启动服务，也可以先下载模型权重，然后传入本地路径。

```bash
# (Optional) Download the checkpoint to a local directory
export MODEL_PATH="$PWD/checkpoints/Xiaomi-Robotics-1"
hf download XiaomiRobotics/Xiaomi-Robotics-1 --local-dir "$MODEL_PATH"

# Launch the inference server (e.g. 8 servers across 8 GPUs, base port 10086)
bash scripts/deploy.sh "$MODEL_PATH" 8 8
# or, without a local download:
# bash scripts/deploy.sh XiaomiRobotics/Xiaomi-Robotics-1 8 8
```

参数依次为 `<model_path> <num_ports> <num_gpus>`。部署脚本从端口 `10086` 起，每个端口启动一个服务，并通过 tmux 的 `model_servers` 会话保持运行。可用以下命令查看。

```bash
tmux attach -t model_servers
```

### 工作流程

- **服务端**使用 `trust_remote_code=True`、Flash Attention 2 和 bfloat16 加载模型，通过带长度前缀的套接字返回动作（`deploy/server.py`）。
- **客户端**使用 `AutoProcessor` 处理并发送观测，收到动作后用 `processor.decode_action(...)` 解码（`deploy/client.py`）。

先启动服务端，再连接客户端。通过仿真客户端完成端到端推理的示例见以下评测指南。

- RoboCasa — [`eval_robocasa/README.md`](../eval_robocasa/README.zh-CN.md)
- RoboCasa365 — [`eval_robocasa365/README.md`](../eval_robocasa365/README.zh-CN.md)
- VLABench - [`eval_vlabench/README.md`](../eval_vlabench/README.zh-CN.md)
