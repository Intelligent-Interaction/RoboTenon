# OpenPI pi0.5（RoboTwin 2.0）

[**English**](README.md) · [简体中文](README.zh-CN.md)

This backend loads `motus-robotics/pi0.5_robotwin2` and uses the checkpoint's joint-state
normalization, three RGB views, and 14-dimensional bimanual actions. The model predicts 32 actions and executes
the first eight before replanning by default.

The backend handles batch=1 for the legacy OpenPI `resize_with_pad_torch`, so no modification
to the OpenPI checkout is required. Example paths for the model and tokenizer are in
[`configs/robotwin2.json`](configs/robotwin2.json).

The full official protocol covers 50 tasks × 100 rollouts per task in `demo_clean` and
`demo_randomized`, totaling 10,000 episodes. Generate the profiles and launch on eight GPUs with
12 workers per GPU and a maximum batch size of 32:

```bash
export SHARED_ROOT=/path/to/resources
export BACKEND_CONFIG=/path/to/local-model-config.json
bash extensions/benchmarks/robotwin/scripts/run_official_full.sh
```

First confirm that a single task and episode returns actions that advance the simulator. For environment checks,
resources, monitoring, and failure recovery, see the [RoboTwin runbook](../../robotwin/RUNBOOK.md).
