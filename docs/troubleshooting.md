# Troubleshooting

[**English**](troubleshooting.md) · [简体中文](troubleshooting.zh-CN.md)

First identify the failing stage: installation, configuration assembly, model startup, task execution, or a Portal query. Record the configuration, `run_id`, task ID, and relevant logs.

## Four checks to run first

After an initial failure, run these checks in order before editing the configuration or installing GPU dependencies.

```bash
# 1. Identify the active Python interpreter. Use Python 3.10 or later.
python -c 'import sys; print(sys.version); print(sys.executable)'

# 2. Check that robotenon comes from this checkout.
python -c 'import robotenon; print(robotenon.__file__)'

# 3. Verify that the main configuration can be assembled.
Tenon modules-check --config configs/config.json
Tenon graphs-check --config configs/config.json

# 4. Run the Fake environment, which has no external service dependencies.
Tenon run-fake --config configs/config.json --output runs/diagnose
```

Use the first failing step to locate the problem.

| Failing step | Likely cause | Where to look |
| --- | --- | --- |
| Steps 1–2 | Virtual environment, Python version, or installation location | Installation and configuration below |
| Step 3 | Configuration paths, component registration, or plugin imports | Installation and configuration below |
| Step 4 | Core runtime failure | Save the complete traceback and the contents of `runs/diagnose` |
| Fake passes, real task fails | Model, simulator, assets, or hardware | Models and simulation below |
| Task succeeds but is missing in the Portal | Entry point, data source, or Portal mode | Results and Portal below |

Start at the exception type at the bottom of the traceback. When reporting a problem, include the full stack so the failing call site remains visible.

## Installation and configuration

| Symptom | What to check |
| --- | --- |
| `No module named robotenon` or `pydantic` | Identify the interpreter with `python -c 'import sys; print(sys.executable)'` and run `python -m pip install -e '.[dev]'` in that environment |
| Python version error | Run `python --version`. The package requires Python ≥ 3.10. Python 3.11 is recommended for development tests |
| `unknown module/tool/provider` | Verify the registered name, installation in the active interpreter, and the modules imported by `plugin_*` |
| Missing component manifest | Resolve its path from the directory of the main configuration file, which may differ from the shell's working directory |
| Graph lacks `outputs.success` | Declare public outputs on the entry Graph for a full episode. See [data interfaces](explicit-graph-data.md#public-graph-outputs) |
| A Tool or Graph disappears after an override | Arrays are replaced as a whole. Check whether the override contains only one entry |

Use commands such as `Tenon modules-check --config <config>` to validate components. To diagnose experiment startup, run `Tenon run --preflight --config <config>`.

## Models and simulation

| Symptom | What to check |
| --- | --- |
| Model endpoint unreachable | Check service logs, loading status, and client host/port. A launch script can exit before the model is ready |
| GPU out of memory | Account for simulation, model, and other processes on the card. Verify physical IDs and Worker-local GPU indices |
| Two GPUs assigned, one in use | Check whether the deployment backend supports sharding. Setting `gpu_ids` alone does not parallelize a model |
| RoboCasa error with `--dry-run` | This option still initializes the environment. Use `run-fake` if no simulator is installed |
| Worker SIGABRT / SIGSEGV | Inspect the fault log and stack for that attempt. Investigate rendering, driver, and model errors separately |
| `resource already reserved` | Find the running process that holds the GPU or robot resource and stop the corresponding experiment normally |

Managed model logs are in `workers/<worker_id>/` under the run directory. Inspect `deployment.json` for the model path and GPU mapping. See the [deployment reference](worker-model-deployment.md#5-ports-lifecycle-and-logs).

## Results and Portal

| Symptom | What to check |
| --- | --- |
| Fake finished but the Portal list is empty | `run-fake` does not register an experiment or produce experiment traces. Submit the CPU example in the [Portal tutorial](tutorials/03-portal.md) |
| Submission is disabled on the read-only home page | The home page lists experiments. With `--no-experiment-submit`, use the [read-only query API](tutorials/cli-experiments.md#5-query-results-over-read-only-http) for CLI artifacts |
| No trace appears | Check `--source`, run identity, time range, and whether the task wrote a trace. The web server queries only the selected database |
| Task finished with zero success | Inspect `task_success`, `reason`, and Graph outputs. A completed workflow can finish without achieving its goal |
| Video is missing | Check recording settings, video dependencies, and whether the file exists. Use `--video-path` for one task and `--save-videos` for a batch |
| Port in use | Connect to the existing Portal or choose another `--port`. Update SSH forwarding to match |
| Status still shows cleanup after stopping | Check cleanup progress. The next experiment can remain queued until resources have been released |
| In-place rerun rejected | Inspect the original configuration snapshot and resource state. Use Fork to create an experiment with a different configuration |
| Historical query timed out or exceeded a limit | Narrow the time range, reduce the page size, and paginate large traces |

## Information to include in a report

Keep the configuration name and overrides, run directory, task and attempt identities, first error with context, runner logs, and model-service logs. Include the corresponding trace and video for image or action failures.

The [change history](changes/README.md) includes past incident reports and reproductions.

## Related procedures

See [Inspecting results](tutorials/view-results.md) for where files and pages should appear, the [Portal tutorial](tutorials/03-portal.md) for web submissions, and [Physical robot deployment](tutorials/real-robot.md) for device drivers and stop interfaces.
