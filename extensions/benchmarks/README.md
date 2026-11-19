# Benchmark evaluation

[**English**](README.md) · [简体中文](README.zh-CN.md)

Evaluate different simulators through a common command. Install and configure the model separately from the environment. Supply a path to a `vla.py` backend to use a model with the existing graph and registry.

The framework's [benchmark guide](../../docs/benchmarks.md) covers installation, evaluation, parallel execution, and results.

## Getting started

| Task | Documentation |
| --- | --- |
| Run a first evaluation | [Quickstart](QUICKSTART.md) |
| Prepare Python environments and shared dependencies | [Installation](SETUP.md) |
| Download benchmark assets and select tasks | Benchmark catalog below |
| Evaluate an HLP, VLA, and verifier together | [System integration](SYSTEMS.md) |
| Integrate a model | [Model interface](models/README.md) |
| Configure multiple GPUs, dynamic batching, scheduling, or timeouts | [Evaluation reference](EVALUATION.md) |
| Modify the runtime or inspect validation records | [Developer guide](benchmark-support/README.md) |

**Evaluation sequence:** Choose an environment, install its dependencies and assets, run a fake-action smoke test, verify the real model on a small subset, then run the full protocol.

## Evaluation inputs

- **Benchmark** selects a simulation environment, such as `libero_plus`.
- **Profile** selects the task set and evaluation protocol, such as `all-0`. Supply a built-in name or an external JSON file.
- **Backend** gives the path to the model's `vla.py`. The model must support the selected environment's observations and actions.

The protocol is independent of the model checkpoint. All backends use the same evaluation entry point.

<a id="installation-and-assets"></a>

## Benchmark catalog

Each benchmark README describes the available profiles. Its setup guide covers simulator dependencies, asset downloads, and local paths. Check the profile before reporting official results because the default may exercise only a subset of the protocol.

| Benchmark ID | Protocol and usage | Installation and assets | Default profile |
| --- | --- | --- | --- |
| `behavior1k` | [BEHAVIOR-1K](behavior1k/README.md) | [SETUP](behavior1k/SETUP.md) | `eval` |
| `calvin` | [CALVIN](calvin/README.md) | [SETUP](calvin/SETUP.md) | `eval` |
| `duobench` | [DuoBench](duobench/README.md) | [SETUP](duobench/SETUP.md) | `smoke_test` |
| `kinetix` | [Kinetix](kinetix/README.md) | [SETUP](kinetix/SETUP.md) | `eval` |
| `libero` | [LIBERO](libero/README.md) | [SETUP](libero/SETUP.md) | `smoke_test` |
| `libero_mem` | [LIBERO-Mem](libero_mem/README.md) | [SETUP](libero_mem/SETUP.md) | `eval` |
| `libero_plus` | [LIBERO-Plus](libero_plus/README.md) | [SETUP](libero_plus/SETUP.md) | `all-0` |
| `libero_pro` | [LIBERO-Pro](libero_pro/README.md) | [SETUP](libero_pro/SETUP.md) | `eval` |
| `maniskill2` | [ManiSkill2](maniskill2/README.md) | [SETUP](maniskill2/SETUP.md) | `eval` |
| `mikasa` | [MIKASA](mikasa/README.md) | [SETUP](mikasa/SETUP.md) | `eval` |
| `molmospaces` | [MolmoSpaces](molmospaces/README.md) | [SETUP](molmospaces/SETUP.md) | `pick_and_place` |
| `rlbench` | [RLBench](rlbench/README.md) | [SETUP](rlbench/SETUP.md) | `eval` |
| `robocasa` | [RoboCasa 0.2](robocasa/README.md) | [SETUP](robocasa/SETUP.md) | `smoke` |
| `robocasa365` | [RoboCasa365](robocasa365/README.md) | [SETUP](robocasa365/SETUP.md) | `smoke` |
| `robocerebra` | [RoboCerebra](robocerebra/README.md) | [SETUP](robocerebra/SETUP.md) | `eval` |
| `robodojo` | [RoboDojo](robodojo/README.md) | [SETUP](robodojo/SETUP.md) | `smoke_test` |
| `robomme` | [RoboMME](robomme/README.md) | [SETUP](robomme/SETUP.md) | `counting` |
| `robotwin` | [RoboTwin](robotwin/README.md) | [SETUP](robotwin/SETUP.md) | `eval` |
| `simpler` | [SimplerEnv](simpler/README.md) | [SETUP](simpler/SETUP.md) | `google_robot_drawer_va-0` |
| `vlabench` | [VLABench](vlabench/README.md) | [SETUP](vlabench/SETUP.md) | `eval` |

BEHAVIOR-1K and RoboDojo require an RTX-capable Isaac environment. Rendering on the required hardware remains unverified. For other environments, consult their setup guides for the scope of verification. Fake-action runs and selected-task probes provide narrower coverage than a full accuracy evaluation.

## Directory layout

```text
extensions/benchmarks/
  benchmark-support/    Evaluation entry point, model loading, graph, and scheduling
  models/               Model backends, which can also live outside the repository
  <benchmark>/
    README.md           Profile selection and environment-specific notes
    SETUP.md            Installation, assets, and initial validation
    configs/            Evaluation protocol JSON files
    src/                Simulator adapter code
```

The legacy `robodojo-office` and `robocasa-hlp` integrations use the commands in their own READMEs.

## Multiple model replicas on one GPU

See [virtual GPU configuration and verification](VIRTUAL_GPUS.md) for regular benchmarks and system graphs.
