# Quickstart: from installation to your first results

[**English**](quickstart.md) · [简体中文](quickstart.zh-CN.md)

This guide takes you through a complete local run.

```text
Install → Run one Fake task → Run a batch of CPU tasks → Inspect the result files
```

No GPU, model weights, simulator, or robot is required. You will verify the core installation and learn where to go next.

## Prerequisites

Before starting, check that you have:

- Python 3.10 or later
- Linux or macOS
- A terminal
- The repository root as the current directory, with `pyproject.toml`, `robotenon/`, and `configs/` present

Check the current directory and interpreter:

```bash
pwd
ls pyproject.toml robotenon configs
python3 --version
```

## Step 1. Install RoboTenon

Create a virtual environment to keep this project's dependencies separate.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Verify that Python imports the code from this checkout.

```bash
python -c 'import sys, robotenon; print(sys.executable); print(robotenon.__file__)'
```

The first line should point to `.venv`. The second should point to `robotenon/__init__.py` in this repository.

> In a new terminal, run `source .venv/bin/activate` again to use the installation.

This installs RoboTenon and the development tools. RoboCasa, MuJoCo, model weights, and robot SDKs are separate. To enable video recording, run `python -m pip install -e '.[dev,video]'`.

## Step 2. Run your first task

```bash
mkdir -p runs/tutorial
Tenon run-fake --config configs/config.json \
  --output runs/tutorial | tee runs/tutorial/result.json
```

The output contains several fields. Check these two first.

```json
{
  "success": true,
  "workflow_status": "completed"
}
```

These values confirm that the installation, configuration loader, and core workflow work together. The command also saves the result in `runs/tutorial/result.json`.

If the command fails, use the [four troubleshooting checks](troubleshooting.md#four-checks-to-run-first). This example has no GPU or simulator dependencies.

## What the command did

`run-fake` exercises the framework locally. It checks the following steps without testing robot performance.

1. Read `configs/config.json`.
2. Load the built-in components selected by the configuration.
3. Create a Fake environment without contacting a device.
4. Execute the steps in the Graph.
5. Print the result and release resources.

The main output fields have the following meanings.

| Field | Meaning |
| --- | --- |
| `success` | Whether this Fake check passed |
| `workflow_status` | Whether the workflow reached its end |
| `session_id` | The ID of the environment session |
| `steps` | Number of Graph steps, which can differ from the number of robot actions |
| `modules` | Application modules loaded for this run |

`--output runs/tutorial` sets the working directory for the Fake environment. The shell command `tee` writes the JSON to a file. This smoke test produces no experiment record, simulation video, or Portal entry.

See the [glossary](glossary.md) for terminology.

## Step 3. Check and run the CPU example

After the Fake smoke test passes, run a full experiment with the tutorial configuration. Start with the preflight check.

```bash
Tenon run --preflight \
  --config docs/tutorials/examples/cpu-demo.json \
  --output-dir data/tutorial/quickstart
```

In the returned JSON, `ok` should be `true`. This confirms that the configuration, directories, and local resources pass startup checks. Models and physical robots require separate validation.

Now run three small CPU tasks.

```bash
Tenon run --batch \
  --config docs/tutorials/examples/cpu-demo.json \
  --run-id quickstart \
  --output-dir data/tutorial/quickstart \
  --save-traces
```

Inspect the summary.

```bash
python - <<'PY'
import json
from pathlib import Path

path = Path("data/tutorial/quickstart/quickstart/summary.json")
summary = json.loads(path.read_text())
for key in ("num_episodes", "num_completed", "successes", "num_errors"):
    print(f"{key}: {summary[key]}")
PY
```

Expected output:

```text
num_episodes: 3
num_completed: 3
successes: 3
num_errors: 0
```

The experiment enumerated the tasks, ran three episodes on Workers, and saved the results and traces to disk. Use a fresh `--run-id`, such as `quickstart-02`, on a second run to preserve the first result.

## Step 4. Choose an interface

The Python API, CLI, and Web Portal share one configuration, runtime, and result format. You can use them together.

### Call the Python API

Use this interface for research scripts, notebook backends, services, or your own scheduler.

```python
import asyncio

from robotenon.runtime import EpisodeRequest, run_episode


async def main() -> None:
    result = await run_episode(
        EpisodeRequest(
            config="docs/tutorials/examples/cpu-demo.json",
            output="data/tutorial/python/episode.json",
            trace_dir="data/tutorial/python/trace",
        )
    )
    print(result["graph_status"], result["task_success"])


if __name__ == "__main__":
    asyncio.run(main())
```

Save this as `run_python.py` and run `python run_python.py`. Expect `completed True`. The example uses multiprocessing, so launch it from a file with the `if __name__ == "__main__"` guard intact. Pasting it into a stdin script such as `python - <<'PY'` does not provide an importable main file. See the [Python API tutorial](tutorials/python-api.md).

### Run from a terminal

The CLI works for servers, automation scripts, and batch evaluation. The CPU example above uses the regular experiment CLI. See the [CLI tutorial](tutorials/cli-experiments.md).

### Run in a browser

Use the Web Portal to submit experiments interactively and inspect progress, trajectories, and video.

```bash
Tenon web \
  --host 127.0.0.1 --port 8765 \
  --project-root . --config-root docs/tutorials/examples \
  --experiment-output-root data/tutorial/portal \
  --run-root data/tutorial/portal \
  --no-auto-grafana
```

Open <http://127.0.0.1:8765/> and choose `cpu-demo.json`. Continue with the [Web Portal tutorial](tutorials/03-portal.md).

## Step 5. Connect a simulator, model, or device

RoboTenon integrates external simulators, models, and devices through extensions. Install the relevant extension, then load it explicitly in the configuration.

| Goal | Next guide |
| --- | --- |
| Install and load an extension | [Using extensions](tutorials/extensions.md) |
| Run RoboCasa with a model | [Your first simulation](tutorials/02-first-simulation.md) |
| Prepare scene assets and model weights | [Download assets and models](tutorials/download-data.md) |
| Connect a physical robot | [Physical robot deployment](tutorials/real-robot.md) |
| Scale to multiple Workers or GPUs | [Eight-GPU deployment](tutorials/server-8gpu.md) |

Each extension's README explains which simulators, GPU builds of PyTorch, model weights, and device SDKs to prepare separately.

## Interpreting future runs

- Run the Fake check first, then validate the simulation or robot.
- `completed` signals that the workflow finished. `task_success` records whether the task goal was achieved.
- The configuration selects the components loaded through the Python API, CLI, or Portal.
- Give each experiment a fresh `run_id` so its results remain available for comparison and diagnosis.
- Consult the [glossary](glossary.md) for unfamiliar terms and [troubleshooting](troubleshooting.md) for errors.

Follow the [tutorial sequence](tutorials/README.md) to continue, or use the [reference guides](README.md#reference) to look up a parameter.

## Migrating an existing installation

Reinstall the framework and its extensions in each Worker, model, and simulator environment. Change the package prefix in imports, plugin factories, and configuration from `robosys` to `robotenon`, and update environment variable prefixes from `ROBOSYS_` to `ROBOTENON_`. Use `Tenon` for shell commands. Restart services so their package and configuration versions match.
