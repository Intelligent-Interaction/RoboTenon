# Python API

[**English**](python-api.md) · [简体中文](python-api.zh-CN.md)

Import `robotenon` to run a single episode or a batch experiment. The API shares configuration loading, component registration, Workers, and artifact formats with the CLI and Portal. It can be embedded in a research script, notebook backend, service, or scheduler.

## 1. Install and import

After installing from the repository root, verify the import:

```bash
python -m pip install -e '.[dev]'
python -c 'import robotenon; print(robotenon.__version__)'
```

The high-level entry points are in `robotenon.runtime`:

```python
from robotenon.runtime import (
    EpisodeRequest,
    ExperimentRequest,
    run_episode,
    run_experiment,
)
```

`run_episode()` and `run_experiment()` are asynchronous. In an async application, use `await`. In a regular script, use `asyncio.run()`.

## 2. Run one episode

Create `run_python.py`:

```python
import asyncio
import json

from robotenon.runtime import EpisodeRequest, run_episode


async def main() -> None:
    request = EpisodeRequest(
        config="docs/tutorials/examples/cpu-demo.json",
        task_id="cpu-demo",
        env_id="fake",
        seed=7,
        output="data/tutorial/python/episode.json",
        trace_dir="data/tutorial/python/trace",
        metrics_dir="data/tutorial/python/metrics",
    )
    result = await run_episode(request)
    print(
        json.dumps(
            {
                "graph_status": result["graph_status"],
                "task_success": result["task_success"],
                "output_path": result["output_path"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
```

Run it from the repository root:

```bash
python run_python.py
```

Expect `graph_status` to be `completed` and `task_success` to be `true`. The configuration uses the `process` backend, so protect the executable section with `if __name__ == "__main__"`. Child processes can import the module again.

Non-`None` fields of `EpisodeRequest` override configuration values. Common fields include `task_id`, `env_id`, `seed`, `horizon`, `model_path`, `graph_id`, `output`, `video_path`, `trace_dir`, and `environment_parameters`. The result is a dictionary. Use `output_path` for the actual output path because attempt directories depend on the backend.

## 3. Run a batch experiment

`ExperimentRequest` enumerates tasks through the configured TaskAdapter, corresponding to the CLI's `--batch` option:

```python
import asyncio
import json

from robotenon.runtime import ExperimentRequest, run_experiment


async def main() -> None:
    result = await run_experiment(
        ExperimentRequest(
            config="docs/tutorials/examples/cpu-demo.json",
            output_dir="data/tutorial/python-batch",
            run_id="cpu-demo-api",
            save_traces=True,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
```

The CPU configuration runs three trials. Look for `num_completed: 3`, `successes: 3`, and `num_errors: 0` in the summary at `data/tutorial/python-batch/cpu-demo-api/summary.json`. Choose a new `run_id` when repeating the experiment to preserve existing artifacts.

## 4. Override parameters

For a single task, use the fields of `EpisodeRequest` directly. In a batch experiment, use `task_adapter_overrides` to select an adapter by configured instance ID or registered name:

```python
request = ExperimentRequest(
    config="path/to/config.json",
    task_adapter_overrides={
        "benchmark": {"max_tasks": 2, "num_trials": 1}
    },
    environment_parameters={"settle_steps": 20},
    output_dir="data/runs",
)
```

Object fields merge recursively. Arrays and scalars are replaced in full. Selecting an unknown adapter raises an error. Supply environment-specific options through `environment_parameters`.

## 5. Resources and errors

- When `run_episode()` or `run_experiment()` returns normally, the runner has cleaned up Workers, environment sessions, observability services, and resource locks.
- In an application with an existing event loop, such as FastAPI or Jupyter, use `await`. Nesting a synchronous `asyncio.run()` call in that loop fails.
- `graph_status == "completed"` records workflow completion. Check `task_success`, `reason`, and `outputs` for the task outcome.
- Record and handle exceptions in the caller. `dry_run=True` is not a substitute for a fake-environment rehearsal. `dry_run` retains the configured environment and can issue commands on physical hardware.

For output fields and traces, see [Inspect experiment results](view-results.md). For the equivalent commands, see [CLI experiments](cli-experiments.md).
