# RoboTenon documentation

[**English**](README.md) · [简体中文](README.zh-CN.md)

Start with the [quickstart](quickstart.md) to run a CPU-only example and see how the pieces fit together.

- **Tutorials** walk you through a task from start to finish.
- **Reference guides** describe interfaces, configuration fields, and runtime behavior.
- **Change history** records upgrades, fixes, and investigations.

## Getting started

1. Follow the [quickstart](quickstart.md) to check your installation in five minutes.
2. Read [Your first run](tutorials/01-first-run.md) to understand a minimal configuration and a Fake task.
3. Choose an interface: the [Python API](tutorials/python-api.md), the [command line](tutorials/cli-experiments.md), or the [Web Portal](tutorials/03-portal.md).
4. [Inspect the results](tutorials/view-results.md) to tell whether the workflow finished and whether the task succeeded.
5. [Use extensions](tutorials/extensions.md) to connect a simulator, model, or device.
6. Run a [RoboCasa simulation](tutorials/02-first-simulation.md) or [deploy to a physical robot](tutorials/real-robot.md).

The [glossary](glossary.md) defines the terminology used throughout the guides.

## Tutorials by goal

| Goal | Tutorial | Outcome |
| --- | --- | --- |
| Check your installation | [Your first run](tutorials/01-first-run.md) | A Fake smoke-test result and configuration checks |
| Embed RoboTenon in a Python program | [Python API](tutorials/python-api.md) | One episode and a batch experiment |
| Run an experiment from the terminal | [CLI experiments](tutorials/cli-experiments.md) | Three CPU tasks, result files, and a trace |
| Run and inspect tasks in a browser | [Web Portal](tutorials/03-portal.md) | An experiment list, task progress, nodes, and trajectories |
| Evaluate a VLA with parallel simulation | [Benchmark guide](benchmarks.md) | A standalone `vla.py`, a common CLI, dynamic batching, and success-rate statistics |
| Install an optional environment or model | [Using extensions](tutorials/extensions.md) | A provider you can load from a configuration |
| Establish whether a task succeeded | [Inspecting results](tutorials/view-results.md) | A way to cross-check result files, Portal pages, traces, and videos |
| Run RoboCasa | [Your first simulation](tutorials/02-first-simulation.md) | One simulated task and a small batch |
| Download scenes and models | [Downloading assets and models](tutorials/download-data.md) | Asset directories and weight files you can verify |
| Control a physical robot | [Physical robot deployment](tutorials/real-robot.md) | Driver and observation integration, plus deployment checks |
| Add a workflow step | [Custom modules](tutorials/04-custom-module.md) | A runnable two-node Graph |
| Evaluate RoboCasa365 across GPUs | [Xiaomi + RoboCasa365](tutorials/benchmark-h100-xiaomi.md) | Eight GPUs, 224 Workers, launch commands, and full results |
| Evaluate VLABench across GPUs | [Xiaomi + VLABench](tutorials/benchmark-h100-xiaomi-vlabench.md) | Eight GPUs, 160 Workers, launch commands, and full results |
| Evaluate LIBERO across GPUs | [Xiaomi Robotics 0 + LIBERO](tutorials/benchmark-h100-xiaomi-libero.md) | Four suites, 2,000 episodes, reproduction steps, and troubleshooting |
| Evaluate LIBERO-Plus across GPUs | [Xiaomi Robotics 0 + LIBERO-Plus](tutorials/benchmark-h100-xiaomi-libero-plus.md) | Eight GPUs, 288 Workers, 10,030 cases, and full results |
| Deploy on a multi-GPU server | [Eight-GPU deployment](tutorials/server-8gpu.md) | Worker, GPU, and model-service placement |

See the [tutorial index](tutorials/README.md) for all walkthroughs.

## Reference

### Running and deploying systems

| Guide | Look here for |
| --- | --- |
| [Benchmark guide](benchmarks.md) | Preparing assets, running a full evaluation, and checking results and throughput |
| [Benchmark parameters](../extensions/benchmarks/EVALUATION.md) | GPU selection, scheduling, batching, recycling, timeouts, and runtime limits |
| [Execution and observability](execution-and-observability.md) | Runtime settings, execution backends, output directories, stopping, and recovery |
| [Worker model deployment](worker-model-deployment.md) | GPU IDs, model ports, processes, and lifecycles |
| [Observation cache](observation-cache.md) | Frame references, caching, persistence, and access across processes |
| [Benchmark migration and validation](benchmark-migration.md) | Configuration entry points for 20 benchmarks, real simulator coverage, and outstanding validation |
| [Upstream dependencies](vendor-guide.md) | RoboCasa, model services, and third-party material |
| [Troubleshooting](troubleshooting.md) | Errors and unexpected results |

### Architecture and development

| Guide | Look here for |
| --- | --- |
| [Benchmark Graph and model contract](native-benchmark-graph.md) | Generated Graphs, the standalone model interface, and its lifecycle |
| [Architecture](architecture.md) | Component responsibilities, call chains, and resource ownership |
| [Module reference](module-guide.md) | Subsystem inputs, outputs, and source entry points |
| [Developer guide](development-guide.md) | Building components and composing configurations |
| [Graph workflows](workflow.md) | Conditions, parallel branches, loops, and recovery |
| [Dynamic batching in a regular Graph](graph-batching.md) | Starting shared models independently of a benchmark, configuring GPUs, batching, and checking results |
| [Explicit Graph data interfaces](explicit-graph-data.md) | Node inputs, outputs, and cross-node references |
| [Node-private context](node-private-context.md) | Read/write isolation, result publication, and checkpoints |
| [Application plugins](plugin_development.md) | Implementing Module, Tool, Body, and Environment providers |
| [Infrastructure plugins](infrastructure-plugins.md) | Extending execution backends, storage, RPC, and observation modalities |
| [Evaluation, monitoring, and task queues](evaluation-monitoring-and-task-queue.md) | Task evaluation, safety interrupts, and queues |

The [architecture report](../robotenon-design-report.xml) presents the design for discussion. The code and reference guides define the supported configuration fields and commands.

## Change history

The [change history](changes/README.md) includes migration guides, fixes, performance measurements, incident investigations, and audit attachments. Test counts in those reports apply to the revisions and environments tested at the time.

## Documentation conventions

- Open each tutorial with its outcome, then list prerequisites and steps.
- Give commands, expected results, and a troubleshooting entry point for each critical step.
- Define new terminology in plain language and link to the [glossary](glossary.md).
- Label examples that can run directly and snippets that illustrate an interface.
- Use repository-relative paths in examples. Use placeholders such as `/opt/envs/...`, `/data/models/...`, and `/path/to/...` for external resources.
- Describe the feature, what its parameters mean, and how to choose them. Omit transient deployment updates, current run status, and chat handoffs.
- Remove personal directories, real hostnames and accounts, private environment names, credentials, and machine-specific configurations from public material, including historical records.
- Document the test conditions and scope for performance figures. Report experimental snapshots as measurements from the stated run.
- Update the corresponding reference page alongside changes to the configuration or CLI. Keep release notes off the landing page.
- Put changes, incidents, and validation evidence in `changes/`. Keep beginner tutorials focused on the task.
- Preserve upstream README and license texts. Document RoboTenon integration in this project's own guides.
