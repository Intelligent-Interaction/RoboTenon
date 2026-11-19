# Reference benchmark runs

[**English**](reference-runs.md) · [简体中文](reference-runs.zh-CN.md)

The following Xiaomi-Robotics-1 evaluations used eight NVIDIA H100 GPUs. Each run completed with zero execution errors and zero cancellations. The linked reports document the commands, environment settings, task-level results, and timing methodology.

| Benchmark | Simulation workers | Completed episodes | Task-macro success | Wall time | End-to-end throughput |
| --- | ---: | ---: | ---: | --- | ---: |
| [RoboCasa365](tutorials/benchmark-h100-xiaomi.md) | 224 | 2,500 | 57.56% | 1 h 46 min 42 s | 23.43 episodes/min |
| [VLABench](tutorials/benchmark-h100-xiaomi-vlabench.md) | 160 | 2,460 | 60.84% | About 1 h 54 min | 21.58 episodes/min |

Task-macro success gives every task equal weight. RoboCasa365 runs the same number of episodes per task, so the macro and episode-weighted rates coincide. VLABench reports 60.84% by task and 61.18% by episode. Each benchmark has its own tasks and step budgets. Use the same protocol when comparing policies.

For additional model and environment combinations, see the [tutorial index](tutorials/README.md). For simulator assets, model dependencies, and how to move from a single-worker smoke test to the full protocol, see the [benchmark guide](benchmarks.md).
