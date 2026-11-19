# Model backend interface

[**English**](README.md) · [简体中文](README.zh-CN.md)

[Benchmark catalog](../README.md) · [First evaluation](../QUICKSTART.md) · [Parallel evaluation](../EVALUATION.md)

This page describes the model implementer's interface. To use an existing model, follow the quickstart to install and smoke-test the simulator, then configure the backend. All native benchmarks share this interface. The backend declares its supported environments and converts model predictions to native actions.

Implement a standalone `vla.py`; pass its file path or `module:factory` to
`Tenon evaluate --backend ...`. File factories use
`create()`. Relative imports such as `from .preprocessing import transform` work
without registry edits, even for two different folders both named `vla.py`.

```python
SPEC = {
    "api_version": 1,
    "supported_benchmarks": ["robocasa365"],
    "supports_batch": True,
    "history": {"length": 4, "interval": 2},
    "replan_steps": 16,
}

def create(**params):
    return VLA(**params)
```

The returned object implements:

- `open_session(session_id, task_info)` — metadata includes `benchmark_id`, stable
  `episode_id`, `task`, `action_spec`, `observation_spec`.
- `act_batch(requests)` — each request has `request_id`, `session_id`, `step_id`
  and `observation`. Observation contains `history` (oldest to newest, sampled
  according to SPEC with initial-frame padding) and `task_description`.
- `close_session(session_id, result)` — release episode caches.
- `close()` — release model resources.

Return one result per request, matched by its original `request_id`:

```python
{"request_id": request["request_id"],
 "action": {"native_actions": [{"actions": action_vector}, ...]}}
```

Native action dictionaries depend on the benchmark and need not be vectors.
Runtime executes at most `replan_steps`, observes every environment step, and
immediately drops cached actions on success/termination. Weights load once per
model replica. Cache and RNG state must be isolated by session. For scheduling
independent RNG use stable episode identity, never worker number or receipt order.

`backend-config` is a JSON object passed unchanged to `create(**params)`.
`SPEC` is static and validated before workers start. Set `supports_batch=False`
for single-only backends; batch size above one is rejected. A batch interface
alone is not evidence of actual batched model computation.

Install shared RoboTenon/runtime in both environments and model dependencies only
in `--model-python`. Install benchmark/simulator dependencies in `--sim-python`.
No cross-environment site-packages injection is needed. The launcher re-executes
in the model interpreter, spawns persistent model services, and runs simulation
under its separate interpreter. Paths refer to the same host filesystem.

[`xiaomi/`](xiaomi/README.md) preserves verified Xiaomi preprocessing, BF16 conversion and both RNG
modes. The system Tool compatibility path forwards to this independent implementation. `fake/` is a protocol fixture, not model inference: configure its `action`
with a native action dictionary valid for your benchmark.

Live profiles use the same batch backend API. Inference may finish after several
environment ticks: observations are queued FIFO, returned action chunks feed the
message stream, and the most recent completed action is selected at the next tick.
Stale ticks use the benchmark's hold action. `close_session` receives `rt_metrics`.
Model-specific continuous-inference/latency-aware strategies require explicit design;
they are not inferred from `supports_batch`.

## HLPs, verifiers, and other non-action models

Full systems use modules, tools, and graphs. VLAs use the interface on this page. Models that return plans or verdicts implement a generic provider. See [system integration](../SYSTEMS.md) and the [fake reasoner example](fake_reasoner/README.md).

## Planning and verification in hierarchical systems

The [Qwen3-VL reasoner](../systems/robocasa_hierarchical/tool/qwen/README.md) provides a resident, batch-capable provider for HLPs and verifiers. It returns structured decisions through the generic BatchProvider interface. Xiaomi outputs actions through its separate `vla.py`. The [hierarchical graph](../systems/robocasa_hierarchical/README.md#managed-deployment-with-real-models) can launch both together.

## Available models

- [Xiaomi Robotics-1](xiaomi/README.md) supports RoboCasa365, dynamic batching, and a separate model environment.
- [OpenVLA-OFT](openvla-oft/README.md) supports the original LIBERO-Spatial benchmark at batch size 1. The documented 500-episode run achieved 98.20% success. See the model guide for verification scope.
