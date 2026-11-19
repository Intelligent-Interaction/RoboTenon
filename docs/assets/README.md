# Documentation images

[**English**](README.md) · [简体中文](README.zh-CN.md)

This directory contains the images used in tutorials and reference guides. Technical diagrams use editable SVG text and connectors. Portal screenshots use PNG to preserve the actual interface.

## Technical diagrams

| Image | What it shows |
| --- | --- |
| [Run paths](diagrams/run-paths.svg) | Installation, CLI, Portal, results, and deployment entry points |
| [Component architecture](diagrams/architecture.svg) | Graph, Environment, Body, Tool, and safety state |
| [Configuration layers](diagrams/config-layers.svg) | Main configuration, overrides, effective configuration, and component assembly |
| [Simulation services](diagrams/simulation-services.svg) | The processes that host RoboCasa, Qwen, and Xiaomi |
| [Demo resources](diagrams/download-resources.svg) | CPU examples, simulation files, and optional demonstration trajectories |
| [Result files](diagrams/result-files.svg) | Summaries, episodes, attempts, and their data sources |
| [Experiment lifecycle](diagrams/experiment-lifecycle.svg) | Submission, queueing, execution, and resource cleanup |
| [Trajectory page navigation](diagrams/portal-navigation.svg) | How to read task details, media, nodes, and call durations |
| [Graph data](diagrams/graph-data.svg) | Input bindings, node outputs, and the Graph's public outputs |
| [Node workspace](diagrams/node-context.svg) | Private execution, commits, and published results |
| [Observation cache](diagrams/observation-cache.svg) | Frame data, cache, and readers |
| [Plugin roles](diagrams/plugin-map.svg) | Extension points for application logic, devices, and infrastructure |
| [Tasks and safety](diagrams/safety-queue.svg) | Evaluator task decisions and Monitor safety stops |
| [Worker GPUs](diagrams/worker-gpu.svg) | Example mapping of GPU IDs for a Worker assigned three cards |
| [Physical robot deployment](diagrams/real-robot.svg) | Models, the control host, observations, drivers, and the stop path |

Connectors indicate responsibilities and data relationships. Consult the accompanying documentation for constraints. The diagrams do not measure performance or specify physical wiring.

## Portal screenshots

These screenshots were captured on 2026-09-21 from an isolated local Portal running three tasks with the [CPU example](../tutorials/examples/cpu-demo.json). They contain CPU-demo data only. Production experiments, simulations, and physical robots were outside this run.

| Image | What it shows |
| --- | --- |
| [Create an experiment](tutorials/portal-create.png) | Selecting a configuration, saving traces, and checking readiness |
| [Experiment list](tutorials/portal-experiments.png) | A CPU experiment that completed all 3 / 3 tasks |
| [Node status](tutorials/portal-nodes.png) | Nodes and their inputs and outputs in the trajectory page's full-screen status panel |
| [Trace query](tutorials/portal-traces.png) | Results of a read-only SQL query against this experiment |

Capture new screenshots when the interface changes. Keep the recorded status, numbers, and outcomes intact. Give each new SVG a title and description, and supply alt text in the document so that readers can find and understand it.

## Project mark

[Vector logo (SVG)](../../robotenon/web/static/tenon.svg) · [512 px avatar (PNG)](robotenon.png)
