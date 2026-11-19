# RoboCasa365 Xiaomi 8 GPU 评测

[English](server-8gpu.md) · [**简体中文**](server-8gpu.zh-CN.md)

8 卡正式 accuracy 评测使用 benchmark-support 的统一 evaluator。
先在 `extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/config.json` 设置 checkpoint 路径和 `rng_mode`。配置由命令生成，
不维护单独的 8 卡 JSON。

```bash
Tenon evaluate --benchmark robocasa365 \
  --profile xiaomi_official --backend ./extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/vla.py --backend-config ./extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/config.json \
  --model-python /envs/model/bin/python \
  --sim-python /path/to/robocasa/python \
  --model-devices 0,1,2,3,4,5,6,7 \
  --workers-per-gpu 16 \
  --batch-size 8 \
  --wait-ms 50 \
  --output runs/xiaomi-official
```

这会运行 `configs/xiaomi_official.json`：target50、每任务 50 episodes、base
seed 7、官方 episode seed、任务自身 horizon、256 像素相机，以及 success
立即结束。每张卡启动一个模型副本和 16 个仿真 Worker；每个 Worker 的
CUDA 与 EGL 物理设备都显式绑定到同一张卡。

GPU 利用率是脉冲式的：仿真 Worker 先渲染和推进，服务只在凑批或 50 ms
窗口到期时执行前向。显存波动来自仿真上下文和模型临时张量，并不表示模型
被反复加载。

正式报告应记录 batch size、wait window、RNG mode、完成/错误 episode 数、
总体、逐任务和逐维度成功率。动态 batch 与 batch size 1 可能有 BF16 数值
差异，因此用单样本基线的统计置信区间比较成功率，不要求逐 episode 浮点一致。

吞吐测试使用 `extensions/benchmarks/robocasa365/tools/xiaomi_speed.py`，不能作为正式
accuracy 结果。
