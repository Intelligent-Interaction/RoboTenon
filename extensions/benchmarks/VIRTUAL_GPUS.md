# Virtual GPUs: multiple model replicas on one physical GPU

[**English**](VIRTUAL_GPUS.md) · [简体中文](VIRTUAL_GPUS.zh-CN.md)

A virtual GPU is a logical execution slot, available to every benchmark and regular process-backed graph.
Each slot has its own model process, inference queue, and assigned simulator workers.
All slots share the physical GPU's compute and memory. The framework does not provide NVIDIA MIG/vGPU hardware isolation.

## Unified evaluation entry point

Map aliases to physical GPUs in the execution configuration. This example assigns two replicas to each of two physical GPUs:

```json
{
  "virtual_gpu_map": {"v0": "0", "v1": "0", "v2": "1", "v3": "1"},
  "model_devices": "v0,v1,v2,v3",
  "workers_per_device": 16,
  "batch_size": 8,
  "wait_ms": 50
}
```

Save the file as `execution.virtual.json` and use the configured backend and simulator environment:

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --backend ./extensions/benchmarks/models/fake/vla.py \
  --execution-config ./execution.virtual.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --output runs/virtual-smoke --max-tasks 2 --episodes-per-task 1
```

Replace the example `vla.py` and `--backend-config` with your model. The fake backend verifies launch settings only.
You can also pass `--virtual-gpu-map '{"v0":"0","v1":"0"}' --model-devices v0,v1 --workers-per-device 16` directly.
The older `workers_per_gpu` and `--workers-per-gpu` remain aliases. If both forms are set, their values must match.
Without a map, existing physical GPU configurations work as before.

The four-slot configuration starts four model replicas and 64 workers. Each replica batches independently
with a maximum batch size of 8 and a 50 ms batching window. Requests from different slots stay separate.
All workers draw from the global episode queue. Virtual device IDs do not affect episode seeds.
Rebuilt workers retain their original slot. Their model services stay resident.
With `official_stream`, replica count and scheduling order may change how random numbers are assigned.

## Multi-model systems

Systems such as Xiaomi + Qwen continue to use their original graph JSON, modules, and tools.
Each entry in `batch_services` must explicitly list every virtual device used by its workers:

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

Replace the example factory and params with the values from the actual system configuration.
The runtime creates a service replica for each virtual slot. It rejects a missing device replica before loading models.
To share one additional model across all slots, explicitly supply its address through `external_batch_services`.

## Regular graph configuration

Set these values in `parameters.runtime.execution`:

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

Routing uses logical IDs. `CUDA_VISIBLE_DEVICES` uses the physical ID, usually exposed as `cuda:0` inside the model process.
Numeric physical GPU IDs are used for EGL assignment by default. Override with actual per-worker EGL indices in `egl_device_ids`.
The evaluation CLI accepts `--egl-device-ids 0,0,0,0`. GPU/MIG UUIDs or nonstandard device enumeration require explicit
EGL indices. MIG rendering capability depends on the hardware and driver.
Leases are deduplicated by physical GPU so different slots on the same device do not request duplicate exclusive leases.
The standalone command `python -m robotenon.runtime.batch_services` also accepts the JSON `--virtual-gpu-map` option.

## Verify assignment and capacity

- `protocol.json` and the generated `config.json` record the map and execution arguments.
- `batch-metrics/*.json` records each service replica's `virtual_gpu_id`, `physical_gpu_id`, and batch metrics.
- Each episode attempt's `status.json` records the worker's logical and physical GPU IDs.
- Child processes can read `ROBOTENON_VIRTUAL_GPU_ID`. Model backends need not interpret it.

Each replica allocates its own weights and inference memory. Rendering also consumes VRAM. Additional replicas may reduce throughput.
Compare peak memory, throughput, and request latency with one and two replicas per GPU before adding more.
Eight logical slots do not guarantee enough memory for eight Xiaomi models.

## Verification scope

Local tests use real child processes and fake providers to verify routing, independent batching and sessions,
CUDA/EGL environment assignment, full graph execution, and worker rebuilding without loading GPU models.
Measure capacity and speed with the actual model on its intended host.
