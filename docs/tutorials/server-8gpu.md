# Eight-GPU Xiaomi evaluation on RoboCasa365

[**English**](server-8gpu.md) · [简体中文](server-8gpu.zh-CN.md)

Use the shared benchmark evaluator for the eight-GPU accuracy run.
Set the checkpoint and `rng_mode` in `extensions/benchmarks/systems/robocasa_hierarchical/tool/xiaomi/config.json`. The command generates the configuration, so
a separate eight-GPU JSON manifest is unnecessary.

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

The command runs `configs/xiaomi_official.json` with target50, 50 episodes per task, base
seed 7, official episode seeds, native task horizons, 256-pixel camera images, and immediate termination
on success. Each GPU hosts one model replica and 16 simulator Workers. The CUDA and EGL
devices for each Worker bind explicitly to the same physical GPU.

GPU utilization is bursty. Workers render and step the environment before sending requests. The model service runs a forward pass
when a batch fills or the 50 ms wait window expires. VRAM can fluctuate as simulation contexts and transient model tensors change
while the model remains loaded.

Report batch size, wait window, RNG mode, completed and errored episodes,
and success rates overall, per task, and per dimension. Dynamic batching and single-sample execution can differ numerically under BF16.
Compare success statistics against a single-sample baseline and its confidence interval.

Use `extensions/benchmarks/robocasa365/tools/xiaomi_speed.py` for a throughput test and report those measurements separately from formal
accuracy scores.
