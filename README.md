# <img src="robotenon/web/static/tenon.svg" width="40" height="40" align="middle" alt=""> RoboTenon

[**English**](README.md) · [简体中文](README.zh-CN.md)

**A modular runtime for executing and evaluating robot policies.**

RoboTenon takes its name from the tenon in a mortise-and-tenon joint. Its workflow graph connects perception, policy, execution, and evaluation through interfaces that let developers replace each component independently. Environment and Body adapters connect the same graph to a simulator or a physical robot. The Python package is `robotenon` and its command-line interface is `Tenon`.

## Run a first experiment

From the repository root on Linux or macOS, use Python 3.10 or later to run the built-in CPU example. It uses a simulated test environment and requires no model weights or robot hardware.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
Tenon run-fake --config configs/config.json --output runs/first-run
```

The command prints a JSON result. A successful run reports `"success": true` and `"workflow_status": "completed"`. The [quickstart](docs/quickstart.md) explains how to validate a configuration and inspect the output.

## System architecture

- **Graphs and modules** define the execution order, conditional branches, and data passed between steps. Tools give modules access to models and other services.
- **Environment and Body adapters** supply observations, execute actions, and report outcomes. Hardware deployments provide their own observation source and driver.
- **Workers** run episodes and manage resources. Multiple workers can share a model service that batches inference requests.
- **Benchmark extensions** define tasks, seeds, action formats, and success criteria. A single policy or a complete system can run under the same benchmark protocol.

## Documentation

| Topic | Guide |
| --- | --- |
| Installation and first experiments | [Quickstart](docs/quickstart.md) · [Python API](docs/tutorials/python-api.md) · [Command line](docs/tutorials/cli-experiments.md) |
| Building a system | [Architecture](docs/architecture.md) · [Graph workflow](docs/workflow.md) · [Plugin development](docs/plugin_development.md) |
| Running on a device or simulator | [Simulation](docs/tutorials/02-first-simulation.md) · [Physical robots](docs/tutorials/real-robot.md) |
| Evaluating policies | [Benchmark guide](docs/benchmarks.md) · [Supported environments](extensions/benchmarks/README.md) · [Reference runs](docs/reference-runs.md) |
| Inspecting execution | [Web portal](docs/tutorials/03-portal.md) · [Results and traces](docs/tutorials/view-results.md) · [Troubleshooting](docs/troubleshooting.md) |

For the full reference and development guides, see the [documentation index](docs/README.md).
