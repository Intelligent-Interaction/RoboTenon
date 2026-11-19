# Evaluating multi-model systems

[**English**](SYSTEMS.md) · [简体中文](SYSTEMS.zh-CN.md)

[Benchmark catalog](README.md) · [Execution options](EVALUATION.md) · [VLA interface](models/README.md) · [RoboCasa system example](systems/robocasa_hierarchical/README.md)

## 1. Choose an integration

- **Single VLA.** Supply `--backend /path/to/vla.py`. The CLI generates a default rollout graph.
- **Full system, such as HLP + VLA + verifier.** Add `--system-config` to use framework modules, tools, and a custom graph.

Both approaches reuse the benchmark Environment, Body, TaskAdapter, episode queue, and metrics. Install the system from this repository's `systems/` directory or from an external Python package. Keep system-specific model loading, preprocessing, and inference in its `tool/` directory. Reference external weights in the configuration.

Custom system graphs currently require a **synchronous profile**. Live profiles such as Kinetix real-time need a dedicated live graph. The default single-model real-time entry point remains available.

## 2. Directory layout and responsibilities

```text
extensions/benchmarks/
  robocasa365/                   # Environment, action specifications, tasks, and protocol
  benchmark-support/            # Shared entry point and evaluation adapters
  models/
    fake_reasoner/              # Test HLP/verifier provider
  systems/
    robocasa_hierarchical/
      pyproject.toml
      SOURCE.json               # Upstream graph revision and migration notes
      tool/
        xiaomi/                 # Action model, preprocessing, and batch inference
        qwen/                   # Planning and verification models and batch inference
      configs/
        system.json             # Native module and tool configuration
        graphs.json             # Native graph definitions
        execution.fake.json     # Fake models and execution resources
        system.qwen.json        # Uses the framework Qwen API tool
        execution.xiaomi.json   # Xiaomi model and execution resources
      module/robotenon_robocasa_system/
        modules.py              # System decision and adapter logic
```

| Layer | Responsibilities | Handled elsewhere |
| --- | --- | --- |
| benchmark/profile | Tasks, seeds, horizons, environment success, and metric dimensions | System planning strategy |
| System configuration / Module | Branches, subgoals, STM, and model output handling | Loading weights and assembling inference batches |
| Tool | Send module requests to a model service and return structured outputs | Simulator success criteria |
| Model provider | Preprocessing, batched inference, model RNG, and session caches | Graph control flow |
| Execution configuration | Workers, GPUs, batch limits, wait windows, and model services | Changes to benchmark protocols |

System configurations reference manifests through `graphs_config`, `modules_config`, `tools_config`, and related fields. Relative manifest paths resolve from the system configuration's directory. Add system-specific parameters under a distinct namespace in `parameters`.

The evaluation CLI protects the Environment, Body, TaskAdapter, task, environment, and benchmark_protocol fields. Only `graph_id` and `monitor_enabled` are accepted in `parameters.runtime`. Set tasks, episodes, and seeds in the profile or through supported evaluation overrides.

## 3. Configuration and commands

See the [RoboCasa system guide](systems/robocasa_hierarchical/README.md) for a complete example. The general command is:

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile smoke \
  --system-config /path/to/system/configs/system.json \
  --execution-config /path/to/system/configs/execution.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --output runs/my-system-smoke
```

`--execution-config` is optional. Explicit CLI options such as `--backend` override execution JSON values. Use snake_case keys such as `batch_size` and `wait_ms`. Backend and backend configuration paths resolve relative to the execution file. The model resolves paths inside its own configuration, so use absolute checkpoint paths.

An execution JSON file accepts the following:

- Existing execution/model options such as `backend`, `backend_config`, `model_devices`, `workers_per_gpu`, `batch_size`, `wait_ms`, `max_retries`, and `scheduler_seed`.
- `batch_services`, a list of framework `BatchServiceConfig` entries for additional services.
- `external_batch_services`, socket addresses for services already running.

Supply `benchmark`, `profile`, `output`, `system_config`, task selection, and episode settings outside the execution JSON.

The main VLA uses the existing backend interface. The service name `vla` and tool ID `policy` are reserved. Choose other names for additional tools and services. Each tool's `params.service` must reference a configured service.

## 4. Dynamic batching in a graph

Each episode executes its graph independently. Only calls to a model tool generate inference requests:

```text
Task A: plan    → planner queue
Task B: action  → VLA queue (no request while a cached action is available)
Task C: verify  → verifier queue
Task D: step    → advance simulator without a model call
```

Requests are grouped by service and compatible batch key. A full batch runs immediately, or a smaller batch runs when its window expires. Tasks may be at different steps. No cross-task barrier is required.

- `batch_size` and `wait_ms` control the VLA managed by the entry point.
- Other services have independent `max_batch_size` and `max_wait_ms` settings.
- Each replica on each GPU maintains its own inference queue.
- Workers prefer a replica on the same GPU. If none exists, they select a replica round-robin by worker index.
- The same graph calls the tool for single samples and batches.
- An API server controls its own batching. Concurrent client requests alone do not establish batched model inference.

A synchronous simulator pauses while waiting for a decision. `max_wait_ms` controls batching only. Queuing, preceding batches, and inference contribute to end-to-end latency. Batch and scheduling changes can affect floating-point results and the order of global RNG draws.

## 5. HLP and verifier models

A VLA retains its `vla.py` interface with `SPEC`, `create()`, and `act_batch()`. An HLP or verifier implements the framework provider contract directly and needs no central registry entry.

```python
class Provider:
    def __init__(self, model_path):
        # Load weights once per service replica.
        self.model = load_model(model_path)
        self.sessions = {}

    def open_session(self, session_id, metadata):
        self.sessions[session_id] = metadata

    def infer_batch(self, requests):
        inputs = [r['observation'] for r in requests]
        outputs = self.model.infer_batch(inputs)  # Native model batch inference
        return [
            {'request_id': r['request_id'], 'output': out}
            for r, out in zip(requests, outputs)
        ]

    def close_session(self, session_id, result):
        self.sessions.pop(session_id, None)

    def close(self):
        self.sessions.clear()
```

Implement `load_model` and `infer_batch` for the chosen model. Install the package in the model environment and configure it as follows:

```json
{
  "batch_services": [{
    "name": "planner",
    "factory": "my_planner.backend:Provider",
    "params": {"model_path": "/models/planner"},
    "devices": ["0", "1"],
    "mode": "batch",
    "max_batch_size": 8,
    "max_wait_ms": 50,
    "max_pending": 256
  }]
}
```

Bind the system tool to the service:

```json
{
  "plugin_tools": ["robotenon_benchmark_support.model_tool"],
  "tools": [{
    "id": "planner",
    "name": "benchmark_model",
    "kind": "model",
    "params": {"service": "planner", "timeout_s": 600}
  }]
}
```

`benchmark_model` supplies module arguments such as the prompt and native image/state observations to the provider:

```text
request.observation = {
  task, prompt, system_prompt, image_roles, ...,
  observation: {images, state, task_description, ...}
}
```

The benchmark defines the observation fields. `image_roles` is a request hint. Each adapter must explicitly select the correct external and wrist cameras for its benchmark.

Each request includes `request_id`, `session_id`, and `step_id`. Responses can arrive in any order, but each request ID must appear exactly once. The framework rejects missing, duplicate, or unexpected IDs. Metadata passed to `open_session` includes the benchmark, stable episode ID, task, action_spec, and observation_spec. Isolate KV caches and STM across episodes.

For incompatible input shapes or decoding settings, implement optional `batch_key(request)` and return a hashable compatibility key. Generic single-sample providers must use `mode: single` and `max_batch_size: 1`. They lack automatic VLA SPEC validation. A loop over individual calls does not constitute batched inference.

The framework calls each tool's `end_session()` on completion, exception, or cancellation. Service disconnection also releases sessions. Implement this hook in custom tools. Module instances may be reused. Store episode state in graph workspaces or model caches keyed by session ID.

## 6. Python environments and service lifecycle

- `--model-python` runs the coordinator and all model services managed by the evaluation.
- `--sim-python` runs workers, system modules/tools, and benchmark environments.
- Keep model packages out of simulator processes. Avoid adding another environment's site-packages to the import path.
- Install the system package in the simulator environment and provider packages in the model environment. Install the framework and benchmark-support in both.

If the HLP and VLA require different Python environments, start a separate service:

```bash
/envs/hlp/bin/python -m robotenon.runtime.batch_services \
  --config /absolute/path/hlp-services.json \
  --ready-file /absolute/path/hlp-ready.json \
  --metrics-dir /absolute/path/hlp-metrics
```

`hlp-services.json` is a JSON **list** of BatchServiceConfig entries. The ready file maps service names to lists of GPU/socket pairs. Supply the socket paths in the execution JSON:

```json
{
  "external_batch_services": {
    "planner": ["/actual/socket/from/ready-file.sock"]
  }
}
```

One address can serve all workers, or specify one address per worker in actual worker order. GPU assignments interleave: 0,1,...,7,0,1,...,7. External service names must be distinct from managed service names, including the reserved `vla`.

The process that starts an external service must stop it. The evaluation command manages only its own services. Local Unix socket services run on the same host. Budget GPU memory for both models and rendering when assigning devices.

## 7. Results and troubleshooting

Outputs include per-episode, per-task, per-dimension, and overall success statistics. `protocol.json.system_config` records the resolved system configuration, and `model_services` records service settings. `config.json` and its graph/module manifests record the effective runtime configuration.

- `simulation.log` records graph, tool, and environment errors.
- `batch-metrics/<service-index>-<replica-index>.json` records requests, batch distributions, and wait times for each service. Index ordering is in `model-service.json`.
- `sessions == 0` after shutdown means the service has no live sessions.
- The verifier may receive fewer requests than the VLA because verification is skipped while cached actions remain.
- If the first action succeeds, the migrated example ends without another verifier call.
- The environment determines `success`. A verifier's `subtask_success` is a separate result.

Record the system graph, models, protocol, RNG, and execution settings for comparisons. An HLP/verifier system and Xiaomi's official VLA-only evaluator exercise different policies even with the same target50 × 50 environment protocol.
