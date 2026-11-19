# 后训练与部署

[English](README.md) · [**简体中文**](README.zh-CN.md)

本目录提供 **Xiaomi-Robotics-1** 的后训练与部署代码。可在自有数据上微调发布的模型权重，并通过与评测相同的客户端–服务端运行时部署模型。

以下命令均在 `xr1/` 目录运行。

---

## 安装

需要 Python 3.9 或更高版本及支持 CUDA 的 GPU。`transformers` 固定为 `4.57.1`，其他版本尚未验证。

```bash
cd xr1
pip install -e .                 # installs assets/requirements.txt
pip install flash-attn --no-build-isolation
```

模型的视觉语言骨干为 `Qwen/Qwen3-VL-4B-Instruct`，需要可从 Hugging Face 或本地缓存加载。

---

## 模型权重

从 Hugging Face 下载 **[Xiaomi-Robotics-1-5B 权重](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-5B)**，保存为 `pretrained_ckpt/model_states.pt`。

---

## 数据

从 Hugging Face 下载 **[XR-1 后训练示例数据集](https://huggingface.co/datasets/XiaomiRobotics/xr1_post_train_demo/tree/main)**。每个 episode 由一份元数据 JSON 和三条同步视频组成（第一人称、左腕、右腕）。完整 JSON 格式见 [数据格式说明](docs/data_format.zh-CN.md)。

默认配置（`configs/data/load_washer.yaml`）读取 `data/json1.json` 至 `data/json5.json` 五个示例 episode，视频位于 `data/videos/`。使用自有数据时，按以下步骤修改配置。

1. 将 `paths` 设为 JSON 文件或目录。目录会被递归扫描。
2. 将 `mean` 和 `std` 替换为打包后相对动作逐步计算的均值与标准差。两者的形状必须为 `(action_length, action_dim)`，默认 `action_length = 30`、`action_dim = 60`。
3. 将 `q01` 和 `q99` 替换为打包后机器人状态的对应分位数。两者的形状必须为 `(state_length, state_dim)`，默认 `state_length = 1`、`state_dim = 60`。

可参考以下示例计算归一化统计量。

```bash
python tools/compute_normalize.py /path/to/training/data -o normalize.json
```

将 `normalize.json` 中的 `mean`、`std`、`q01` 和 `q99` 复制到数据配置中。

视频路径从各 JSON 文件中读取。`crop_bbox` 顺序为 `(top, left, height, width)`，也可设为 `null`。

---

## 训练

登录 Weights & Biases 后，使用启动脚本开始训练。

```bash
wandb login

RESOURCE_GPU=1 bash scripts/train.sh \
  trainer.project="xiaomi-robotics-1" \
  trainer.exp_name="posttrain" \
  trainer.default_root_dir="outputs" \
  data=load_washer \
  model=posttrain \
  model.params.pretrained="pretrained_ckpt/model_states.pt"
```

`RESOURCE_GPU` 表示本机 GPU 数量。多节点运行时，启动器还直接读取 `WORLD_SIZE`、`RANK`、`MASTER_ADDR` 和 `MASTER_PORT`。默认启用异步训练。传入 `model.params.model.async_train=false` 可切换为同步训练。

权重与解析后的配置保存在 `outputs/project_<project>/<exp_name>/`，例如 `outputs/project_xiaomi-robotics-1/posttrain/`。

---

## 部署

使用推理服务加载训练后的权重，需要安装 `tmux`。

```bash
bash scripts/deploy.sh outputs/project_xiaomi-robotics-1/posttrain 1 1
```

最后两个参数依次是 `<num_ports> <num_gpus>`，端口从 `10086` 开始。使用 `tmux attach -t model_servers` 查看服务。

### 客户端 API

`mibot/server/runtime/client.py` 接收三张 PIL 图像、一条指令和当前机器人状态，并返回 `raw_action`、`action_components` 和 `action_targets`。

机器人状态包含 `left/right_arm_joint`、`left/right_gripper_pos`、`left/right_ee_pos` 与 `left/right_ee_rotm`，以及可选的 `waist_pos`。异步执行时，可通过 `action_prefix=...` 传入形状为 `(N, 60)` 的未归一化动作前缀。可选的 `crop_bboxes=...` 接受三个 `(top, left, height, width)` 元组。

---

## 许可证

本项目采用 [Apache License 2.0](../LICENSE)。
