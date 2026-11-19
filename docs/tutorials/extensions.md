# Use benchmark extensions

[**English**](extensions.md) · [简体中文](extensions.zh-CN.md)

Each benchmark is a separate Python package providing its simulation protocol, Environment, Body, and
TaskAdapter. All extensions follow the same public structure and use the shared core runtime.

## 1. Select and install an extension

For example, install LIBERO as follows:

```bash
python -m pip install -e . -e extensions/benchmarks/benchmark-support -e extensions/benchmarks/libero
python -m pip install -r extensions/benchmarks/libero/requirements.txt
```

Each extension README describes the simulator, GPU packages, assets, datasets, and device SDKs it needs.
Use separate environments for simulators with conflicting dependencies. The Worker's Python interpreter must
be able to import the core package, `benchmark-support`, and the extension.

## 2. Connect a model

The model entry point is:

```text
extensions/benchmarks/<benchmark>/src/robotenon_<benchmark>/vla.py
```

Keep `create()` and implement `start_episode()`, `act()`, `end_episode()`, and optionally
`close()`. The default fake VLA checks the interface and simulator stepping.

## 3. Run the evaluation

Select a profile directly under the extension's `configs/` directory:

```bash
Tenon evaluate --benchmark libero \
  --backend /path/to/my_model/vla.py --model-python /path/to/model/python \
  --sim-python /path/to/libero/python \
  --profile extensions/benchmarks/libero/configs/spatial.json \
  --output runs/libero
```

`evaluate.py` generates the complete master, Graph, and Module configuration under the output directory and runs it through
the standard experiment runner. Separate copies of `native_test.json`, the graph manifest, and the Portal configuration
are unnecessary.

Results are written to:

```text
runs/libero/config.json
runs/libero/config_graphs.json
runs/libero/config_modules.json
runs/libero/runs/<run-id>/summary.json
```

The summary reports overall, per-task, and per-dimension success, errors, and completeness.
Use the upstream profile identified in the README for formal reports. Short smoke profiles check installation
and action flow.

## 4. Batch inference

A `vla.py` backend with batch support can also expose `create_batch()`. Configure simulator Workers, model replicas,
batch size, and wait time with the shared generator:

```bash
python -m robotenon_benchmark_support.configure \
  --profile extensions/benchmarks/libero/configs/spatial.json \
  --batch-interface batch --inference-mode batch \
  --workers 8 --worker-devices 0,0,1,1,2,2,3,3 \
  --model-devices 0,1,2,3 --batch-size 8 --batch-wait-ms 50 \
  --output runs/libero-batch/config.json
```

Dynamic batching changes model scheduling. Each simulator Worker advances its own task in order.
Isolate model history and action caches by session ID.

## 5. Runtime-specific requirements

- BEHAVIOR-1K and RoboDojo use different Isaac Sim stacks. Each requires a supported RTX host and
    authorized access to its assets.
- RLBench requires a compatible CoppeliaSim installation and the applicable license.
- The Kinetix realtime profile needs a dedicated live Graph. The synchronous evaluator rejects it.
- RoboCasa365 provides separate Xiaomi accuracy and throughput entry points.

See [Graph and VLA contracts](../native-benchmark-graph.md) for the full protocol and the
[benchmark catalog](../benchmark-catalog.json) for profile listings. Refer to each extension's README for its
installation and commands.
