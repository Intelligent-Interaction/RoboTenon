# 虚拟 GPU：同一实体卡运行多个模型副本

[English](VIRTUAL_GPUS.md) · [**简体中文**](VIRTUAL_GPUS.zh-CN.md)

虚拟 GPU 是框架的逻辑执行槽位，适用于全部 benchmark 和普通进程 Graph。
每个槽位有独立模型进程、推理队列和固定绑定的仿真 Worker。
它不提供 NVIDIA MIG/vGPU 的硬件隔离，不增加显存或算力。

## 统一评测入口

在执行配置中声明别名到实体 GPU 的映射。例如两张实体卡，各运行两个副本：

```json
{
  "virtual_gpu_map": {"v0": "0", "v1": "0", "v2": "1", "v3": "1"},
  "model_devices": "v0,v1,v2,v3",
  "workers_per_device": 16,
  "batch_size": 8,
  "wait_ms": 50
}
```

保存为 `execution.virtual.json`，沿用已配置好的后端和仿真环境：

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --execution-config ./execution.virtual.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --output runs/virtual-smoke --max-tasks 2 --episodes-per-task 1
```

根据目标模型改用实际 `vla.py` 和 `--backend-config`；此命令用于验证启动配置，fake 不代表模型准确率。
也可直接传 `--virtual-gpu-map '{"v0":"0","v1":"0"}' --model-devices v0,v1 --workers-per-device 16`。
旧 `workers_per_gpu` / `--workers-per-gpu` 保留为别名；两者同时设置时必须相同。
未配置映射时，原实体 GPU 配置保持兼容。

上述四槽位配置共创建 4 个模型副本、64 个 Worker。每个副本独立组 batch，
最大 batch 为 8、最长组 batch 等待为 50 ms。不同槽位不会合并 batch。
全局 episode 队列仍由所有 Worker 共同领取；episode seed 不受虚拟设备编号影响。
Worker 重建后仍绑定原槽位，模型服务保持常驻。
`official_stream` 模式的随机数分配可能随副本数量和调度顺序改变。

## 多模型系统

Xiaomi + Qwen 等系统继续使用原 Graph JSON、Module 和 Tool。
执行配置的每个 `batch_services` 服务都需要显式列出所有 Worker 使用的虚拟设备：

```json
{
  "name": "qwen",
  "factory": "your_package:YourProvider",
  "devices": ["v0", "v1", "v2", "v3"],
  "mode": "batch",
  "max_batch_size": 8,
  "max_wait_ms": 50
}
```

这里的 factory 是占位示例，替换为系统原配置中的真实 factory 和 params。
每个虚拟槽位分别创建一份该服务。缺少某个虚拟设备的服务副本会在加载模型前报错。
如需所有槽位共享一个额外模型，使用现有 `external_batch_services` 显式提供服务地址。

## 普通 Graph 配置

在 `parameters.runtime.execution` 中设置：

```json
{
  "backend": "process",
  "max_workers": 4,
  "virtual_gpu_map": {"v0": "0", "v1": "0"},
  "worker_devices": ["v0", "v1", "v0", "v1"],
  "batch_services": [{
    "name": "model",
    "factory": "your_package:YourProvider",
    "devices": ["v0", "v1"],
    "max_batch_size": 8,
    "max_wait_ms": 50
  }]
}
```

模型路由使用逻辑 ID；`CUDA_VISIBLE_DEVICES` 使用实体 ID，模型进程内一般使用 `cuda:0`。
数值实体 GPU ID 默认用于 EGL 绑定。可通过 `egl_device_ids` 覆盖为每个 Worker 的实际 EGL 索引；
统一入口对应 `--egl-device-ids 0,0,0,0`。使用 GPU/MIG UUID 或特殊渲染设备枚举时应显式配置
EGL 索引；MIG 是否支持所需渲染取决于实际硬件与驱动。
资源租约按实体 GPU 去重，同卡不同槽位不会重复申请独占资源。
独立模型服务命令 `python -m robotenon.runtime.batch_services` 同样支持 `--virtual-gpu-map` JSON 参数。

## 排查绑定与容量

- `protocol.json`、生成的 `config.json` 保留映射和执行参数。
- `batch-metrics/*.json` 记录每个服务副本的 `virtual_gpu_id`、`physical_gpu_id` 和 batch 指标。
- 每个 episode attempt 的 `status.json` 记录 Worker 的逻辑/实体 GPU ID。
- 子进程可读取 `ROBOTENON_VIRTUAL_GPU_ID`，无需模型后端解释该字段。

多个副本分别占用模型权重和推理显存，仿真渲染也占显存。增加副本不保证更快。
先比较每卡 1、2 个副本的峰值显存、吞吐和请求延迟，再增加数量。
8 个逻辑槽位不意味着一张卡可以装下 8 个 Xiaomi 模型。

## 验证边界

本地测试使用真实子进程与 fake provider，验证路由、独立 batching/session、
CUDA/EGL 环境绑定、完整 Graph 执行与 Worker 重建；不加载真实 GPU 模型。
真实模型容量和速度需要在目标服务器单独测量。
