# Xiaomi Robotics 0

[**English**](README.md) · [简体中文](README.zh-CN.md)

This backend evaluates the official
[`XiaomiRobotics/Xiaomi-Robotics-0-LIBERO`](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-0-LIBERO)
checkpoint on LIBERO through RoboTenon's unified benchmark runner.

It reproduces the released evaluator's two-camera prompt, 32-dimensional
state layout, stable per-observation random seed, continuous 7-DoF actions,
and 10-step replanning interval. It also batches independent simulator
requests while preserving the official random noise for every observation.

Download the checkpoint to the `model_path` configured in
`configs/libero.json`, install `requirements.txt` in a model environment, and
select this directory as the runner's model plugin.

## Before a full run

- Keep the model environment separate from each simulator environment. Base
  LIBERO, LIBERO-Plus, and LIBERO-Pro use overlapping package names but
  different simulator sources and assets.
- Run one episode with batch size 1 first. A process reaching `completed` does
  not imply task success; inspect `task_success`, `reason`, image shapes, and
  action values.
- The batch adapter requires both 256×256 `agentview` and `wrist` images plus
  the 8-dimensional LIBERO state. Missing wrist images or discretized gripper
  actions are interface errors, not low benchmark scores.
- `replan_steps=10`, continuous 7DoF actions, and the per-observation noise seed
  are part of the model interface. Changing them creates a different result.
- Do not commit machine-local checkpoint paths, account names, hostnames, or
  run directories. Copy the JSON into a local ignored directory and use
  portable placeholders in public documentation.

Reader-oriented examples, measured results, and troubleshooting:

- [Base LIBERO](../../../../docs/tutorials/benchmark-h100-xiaomi-libero.md)
- [LIBERO-Plus](../../../../docs/tutorials/benchmark-h100-xiaomi-libero-plus.md)
