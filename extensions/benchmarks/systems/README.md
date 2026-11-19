# Systems under evaluation

[**English**](README.md) · [简体中文](README.zh-CN.md)

Systems use framework **Module / Tool / Graph** components to express decision logic. Model inference and weights are managed through [model backends](../models/README.md). Each benchmark extension defines its environment and evaluation protocol.

- [System integration guide](../SYSTEMS.md) covers layout, configuration, dynamic batching, model services, isolated environments, and results.
- [Hierarchical RoboCasa system](robocasa_hierarchical/README.md) migrates the planning/VLA/verification graph from a pinned revision and uses the current RoboCasa365 Environment, Body, and TaskAdapter.

Package a new system independently with `configs/`, `src/<package>/modules.py`, and a README. Load it through plugin_modules / plugin_tools without modifying the framework registry. External configurations can be referenced by absolute path.

## Available configurations

| System | Configuration | Verification |
|---|---|---|
| Xiaomi + Qwen3-VL hierarchy | `robocasa_hierarchical/configs/system.qwen_batch.json`, loaded by the unified evaluator | Short real-model runs validated single-sample and dynamic batching on separate and shared GPUs. Full accuracy is evaluated separately. |
| Fake hierarchy | `system.json` + `execution.fake.json` | Real processes and graph execution tested with fake outputs |
| Xiaomi + external Qwen API | `system.qwen.json` | Legacy HTTP integration. Start the API separately. |

The VLA-only Xiaomi backend uses the evaluator's default rollout graph. See the hierarchical system README for complete installation, smoke, and full-run commands.
