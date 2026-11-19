# Hierarchical RoboCasa system: HLP → VLA

[**English**](README.md) · [简体中文](README.zh-CN.md)

[System integration](../../SYSTEMS.md) · [RoboCasa365 installation and assets](../../robocasa365/SETUP.md) · [RoboCasa365 protocols](../../robocasa365/README.md)

## Origin and scope

The migration uses revision `af74484a88d9fddd225e35ed211f50c8103172b3` of
`extensions/robocasa365/configs/robotenon_xr365_graphs.json`.

Two graph definitions are provided:

- `xr365_single_task_reference` is the default and has no monitor node.
- `xr365_single_task` includes `monitor.check`. Declare and enable monitors explicitly in the system configuration to use it.

The graphs retain the original subgoal instructions, STM, verification, and action-queue branches. The production Qwen configuration uses a general progress-planning module. Reference `plan` and `verify` modules remain available for compatibility and comparison.

Environment, Body, and TaskAdapter **all come from the current `robocasa365_benchmark`**. The legacy environment.py, body.py, tasks.py, Xiaomi socket tool, and horizon=16 configuration are outside this integration.

The migration makes the following changes:

1. Each graph action node executes one native action. The tool caches the returned chunk, so each observation collected during its execution enters the VLA history.
2. With `needs_plan=False`, the graph continues executing actions and verification skips inference. Verification and replanning resume after the queue is exhausted.
3. After observation, `terminal_guard` ends the episode immediately on environment success or horizon, skipping further verifier/HLP/VLA calls.
4. Outputs include per-episode metrics and dimensions from the current benchmark. Visual subtask verification remains separate from environment success.
5. The graph execution limit is 1,000,000 nodes. The actual simulation step limit follows the profile and each task's horizon.
6. A planner stop still requires confirmed environment success. The original branch remains, while terminal_guard ends normally successful episodes early.
7. The production Qwen configuration uses no task list or task-specific rules. STM stores the current subgoal. By default it executes three action chunks before comparing planning frames. `continue` reuses the subgoal exactly, and only `advance` or `recover` may replace it.

These changes can affect graph nodes, image trajectories, and model call timing relative to the earlier version. [SOURCE.json](SOURCE.json) records the source commit and migration scope.

## Installation

Run the following commands at the RoboTenon repository root. Follow the RoboCasa365 setup guide to install the simulator and download assets first. Install packages separately in the model and simulator environments:

```bash
# Model environment: framework and test provider.
/envs/model/bin/python -m pip install -e . \
  -e extensions/benchmarks/benchmark-support \
  -e extensions/benchmarks/models/fake_reasoner

# Simulator environment: framework, benchmark environment, and system modules.
/envs/sim/bin/python -m pip install -e . \
  -e extensions/benchmarks/benchmark-support \
  -e extensions/benchmarks/robocasa365 \
  -e extensions/benchmarks/systems/robocasa_hierarchical
```

If both components share one Python environment, combine their packages in a single installation. The system package does not download assets or weights.

## First run with three fake models

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile smoke \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.fake.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --model-devices 0 \
  --output runs/robocasa-system-fake-smoke
```

The configuration uses a zero-action VLA, fake planner, and fake verifier to check image, request, action, and metrics handling. RoboCasa rendering still requires a GPU. Omitting model-devices leaves device assignment unspecified.

`execution.fake.json` defaults to two workers, VLA batch size 2, and a 50 ms batching window. The fake planner and verifier each have a service with no explicit GPU assignment. The fake VLA uses replan_steps=1 and verifies every step. The real Xiaomi model uses replan_steps=16 and exercises the cached-action branch.

## Managed deployment with real models

The **Xiaomi + Qwen3-VL** integration loads both models as persistent services. The unified evaluator loads the system configuration and graph. The production configuration has no separate visual verifier. It checks native success/terminal after each action. At chunk boundaries it reuses the current subgoal or asks the HLP for a progress check at the configured interval.

Configuration files:

| File | Contents |
|---|---|
| `configs/system.qwen_batch.json` | HLP planner → Xiaomi action chunk → environment termination check |
| `configs/smoke.real.json` | One episode for each of two tasks with a 32-step cap, allowing multiple 16-step action chunks |
| `tool/qwen` | The Qwen3-VL model provider in this system's tool directory |

**Verification:** Real Xiaomi + Qwen completed a two-task smoke test (2/2, zero errors) and a split-GPU concurrency check (16/16 episodes, zero errors). Both models produced multi-sample batches. The 32-step tests verify integration. Shared-GPU capacity is recorded separately, and task success requires a full evaluation.

Install the framework, benchmark, and system packages described above together with [Qwen dependencies](tool/qwen/README.md) and [Xiaomi dependencies](tool/xiaomi/README.md). Install each package in its intended environment without sharing site-packages.

### Module / Tool / Graph

| Layer | Declaration or implementation | Responsibility |
|---|---|---|
| Module | `module/robotenon_robocasa_system/modules.py` | General keep/advance/recover planning, actions, and termination. The production Qwen verify node forwards without inference. |
| Tool | Planner in `configs/system.qwen_batch.json` and generated policy | HLP calls Qwen through benchmark_model. The policy calls Xiaomi through benchmark_backend. |
| Graph | `configs/graphs.json`, referenced by system.qwen_batch.json | reset → plan → action → body_execute → observe → terminal_guard. After a chunk, plan may reuse the subgoal without calling Qwen. |
| Service deployment | `configs/execution.xiaomi_qwen.json` | One Xiaomi and one Qwen replica per GPU, 16 workers, batch limit 8, 50 ms batching window |

The tools reuse model RPC and history management from benchmark-support. Model implementations and weight handling live under models. The system composes them using modules, tools, and graphs.

### Launch the graph with the unified evaluator

Set the checkpoint path in `tool/xiaomi/config.json` and the Qwen `model_path` in `configs/execution.xiaomi_qwen.json`. Paths below are relative to the repository root.

```bash
Tenon evaluate \
  --benchmark robocasa365 \
  --profile extensions/benchmarks/systems/robocasa_hierarchical/configs/smoke.real.json \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.qwen_batch.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.xiaomi_qwen.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/robocasa/bin/python \
  --output runs/xiaomi-qwen-smoke
```

The evaluator starts the graph, model services, and simulator. The default deployment uses eight GPUs. For a one-GPU check, set execution `model_devices` to `"0"` and reasoner `devices` to `["0"]`. Matching assignments give workers local access to both services. Separate GPU sets are also supported.

In `runs/evaluation/summary.json`, expect two completed smoke episodes and zero errors. Check actual batch sizes in `batch-metrics` and sessions=0 after shutdown. Two tasks cannot verify batch-8 capacity. Increase concurrency with `--episodes-per-task 8`. Set `metrics_dir` in reasoner params to inspect Qwen outputs.

After the smoke test, select `xiaomi_official` with `--profile` and use a new output directory for target50 × 50. This uses official task horizons and seeds without the 32-step cap. Report it as a **Xiaomi + Qwen hierarchical system result**. Record actual GPUs, batch settings, and RNG. The 32-step smoke result only describes the short integration run.

The unified evaluator owns resource lifecycles. Modules, tools, and graphs implement runtime logic.

## Optional external Qwen HTTP service

Prepare the following:

1. Install [Xiaomi dependencies](tool/xiaomi/requirements.txt) and download the RoboCasa365 checkpoint. Provide the actual `model_path` and RNG mode in a separate backend JSON.
2. In a separate process, start an OpenAI-compatible chat completions service serving the Qwen vision-language model.
3. Set `QWEN_ENDPOINT` and `QWEN_MODEL`. If authentication is required, supply `QWEN_API_KEY` through the environment and keep it out of configurations and reports.
4. Prepare assets using the RoboCasa365 setup guide, then verify a small task subset.

The supplied `system.qwen.json` uses the framework's `Qwen3.5-VL` tool for planning and the unified service for the VLA. The visual verifier defaults to off.

```bash
export QWEN_ENDPOINT=http://127.0.0.1:8000/v1/chat/completions
export QWEN_MODEL=/models/Qwen3.5-9B

Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.qwen.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.xiaomi.json \
  --backend-config /absolute/path/xiaomi-backend.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --model-devices 0 --workers-per-gpu 2 --batch-size 2 \
  --max-tasks 2 --episodes-per-task 1 \
  --output runs/robocasa-system-real-smoke
```

The Qwen API tool runs an HTTP client in the simulator process, reads images from the observation cache, and sends them to the server. The simulator does not load the weights. Server-side batching depends on the API server. This configuration guarantees framework dynamic batching for the Xiaomi VLA only.

To batch all three models through the framework, use `system.json`, replace both test providers in `execution.fake.json` with real HLP/verifier providers, and select the Xiaomi VLA backend. See the [provider interface](../../SYSTEMS.md#5-hlp-and-verifier-models). Report a system with fake HLP/verifier components separately.

### Full protocol on eight GPUs

After a real-model smoke test, remove the `--max-tasks` and `--episodes-per-task` limits and the smoke GPU/worker overrides:

```bash
Tenon evaluate \
  --benchmark robocasa365 --profile xiaomi_official \
  --system-config extensions/benchmarks/systems/robocasa_hierarchical/configs/system.qwen.json \
  --execution-config extensions/benchmarks/systems/robocasa_hierarchical/configs/execution.xiaomi.json \
  --backend-config /absolute/path/xiaomi-backend.json \
  --model-python /envs/model/bin/python \
  --sim-python /envs/sim/bin/python \
  --output runs/robocasa-system-target50x50
```

This execution configuration starts eight VLA GPU replicas, 16 simulator workers per GPU, VLA batch size 8, and a maximum 50 ms batching window. Allocate resources for Qwen separately. Check capacity before sharing these eight GPUs.

The environment protocol remains target50 × 50 with official seeds and task horizons. **This measures the hierarchical system.** For Xiaomi's VLA-only result, omit `--system-config` from the unified evaluation command.

## Customizing a system

- To change control flow, edit `configs/graphs.json` using graph conditional edges and goto.
- To change planning, register a new module through plugin_modules and reference it in the graph.
- To change models, update tool-service bindings or the provider factory. Load weights in the provider.
- To change inference resources, edit the execution JSON. Other models' max_batch_size settings are independent of top-level batch_size.
- To use another verifier, set `parameters.model.verify_tool`. When unset, the system reuses hlp_tool.
- To change benchmarks, select the appropriate benchmark/profile and adapt model inputs and actions explicitly. The example targets RoboCasa365.

## Local validation

`tests/test_benchmark_systems.py` uses real framework processes, graphs, shared model services, and a deterministic simulator-contract fixture to verify:

- System configuration preserves the current benchmark Environment, Body, TaskAdapter, and protocol.
- System flow is consistent for single-sample and dynamically batched requests.
- Every observation enters history while a cached action chunk executes.
- Success ends the episode immediately without an additional verifier call.
- Tasks finish independently and other work continues.
- Failed episodes reach the exact horizon. Out-of-order responses remain associated with the correct session.
- Services release sessions normally and simulator processes do not import models.

Run smoke and full evaluations on a host with assets, models, and GPUs to verify actual RoboCasa rendering, Xiaomi inference, and Qwen service behavior.

## Regression against af74484a

The core planner/verifier follows the pinned revision. The Qwen tool parses complete JSON and the module handles fields. The integration retains current benchmark interfaces, per-step observations, immediate termination on success, and dynamic batching. terminal_guard and metric outputs are explicit benchmark adaptations. The current Qwen3-VL-4B and reference Qwen3.5 are different models. Contract alignment does not establish equivalent trajectories or success rates.

The two-task smoke test missed planner field variants seen in full runs. Retest with `configs/smoke.af74484a.json` across target50, one episode per task and a 64-step cap. Use the full path for `--system-config configs/system.qwen_batch.json` and the unified evaluator. Expect 50 unique episodes and zero errors/cancellations. Inspect actual batch sizes, planner/verifier calls, raw errors, and session cleanup. This checks integration only.

The regression profile retains the official seed stride of 50. Validate real weights, assets, and rendering before running the full 50 × 50 xiaomi_official protocol.

## Training-free contextual planning experiment

`configs/system.contextual.json` uses the existing Qwen and Xiaomi weights without training or fine-tuning.
Qwen generates Xiaomi's next instruction from the original task, previous and current camera views, and recently executed instructions.
It may select the full original task, reuse the current instruction, or generate a contextual instruction. Atomic decomposition is optional.
Use this configuration for paired experiments. An improvement over Xiaomi alone has yet to be demonstrated.

Another experimental configuration, `configs/system.advisory.json`, preserves the original task as the prefix of Xiaomi's instruction.
Qwen returns `{"memory": "scene facts and uncertainty", "guidance": null}` or supplies nonempty visual guidance.
With null guidance, Xiaomi receives the original task. Otherwise it receives the original task, a newline, and the supplementary guidance.
Qwen may retract guidance on a later call. Memory is isolated per episode and is available only to Qwen.
This mode uses `policy.planner_mode=advisory` and preserves invocation frequency, graph, action history, and termination semantics.
It uses no task-specific branches, manually chosen stall threshold, or training. Preserving the original text does not guarantee equal or higher success.
Run the same evaluator and select this configuration with `--system-config`.

- `policy.planner_mode=contextual` enables contextual planning with output schema `{"instruction": "..."}`.
- `policy.progress_check_interval_chunks=3` calls Qwen after three action chunks.
  This setting controls inference frequency. Smaller values increase visual feedback and inference cost without defining stalls or success.
- Episode STM stores instructions and actual executed chunk counts for the latest eight planning intervals.
  These records describe commands executed, regardless of success. The previous planning image shows the scene before the most recent record.
- Each Xiaomi prediction still receives the latest observation and its original history sampling. Environment success determines termination.
- Invalid model output raises an explicit error.

For comparison, rerun the VLA-only Xiaomi baseline with the same tasks, episode seeds, horizons, and model settings.
Use `session_deterministic` and VLA batch size 1 to reduce scheduling and numerical confounds.
This is a controlled comparison and is reported separately from official `official_stream` results. Complete all paired episodes and report both success rates,
system-only successes, and Xiaomi-only successes. Validate on independent seeds, then assess dynamic-batch throughput and accuracy separately.

This design draws on execution feedback in [Inner Monologue](https://arxiv.org/abs/2207.05608).
That work used its own skills and feedback modules. The benefits of combining Qwen with Xiaomi require direct measurement.

Use the read-only statistics command for paired results:

```bash
python -m robotenon_benchmark_support.paired_results \
  runs/xiaomi-baseline runs/xiaomi-contextual > runs/paired-results.json
```

Both arguments point to evaluator output directories. The command checks protocol, model parameters, and episode lists for agreement.
It computes success-rate differences only after both runs complete all planned episodes, with a task-cluster bootstrap 95% interval.
Overall and `per_task` outputs report `both` (both succeed), `system_only` (only the system succeeds),
`baseline_only` (only the baseline succeeds), and `neither` (both fail), so a net rate cannot conceal gains and regressions.
`baseline_success_retention_rate` is the fraction of baseline successes also solved by the system.
`baseline_failure_recovery_rate` is the fraction of baseline failures solved by the system.
A zero denominator yields `null`. Here recovery describes paired outcomes and implies no particular recovery action.
`execution_differences` lists batch and scheduling differences for identifying additional confounders.
The interval measures uncertainty within the current test set. Independent-seed validation remains necessary.
Identical seeds may still yield different upstream scenes. The statistics command checks declared protocols and episode identities
and records this limitation as `initial_state_alignment=not_verified`. For rigorous pairing, compare initial images,
robot state, and scene metadata. First verify action parity using a system that retains the original instruction.
If you change upstream scene sampling order for diagnosis, report the diagnostic protocol separately from official evaluator results.
When Qwen `metrics_dir` is set, each service writes request-level decisions to `*.decisions.jsonl`.
Records include session, task, prompt, model output, and input image count. They contain no images or weights.
