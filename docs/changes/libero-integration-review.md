# LIBERO integration reassessment (2026-09-20)

[**English**](libero-integration-review.md) · [简体中文](libero-integration-review.zh-CN.md)

This source review followed the workspace's decoupling changes. It did not install or run LIBERO or modify the integration.
Upstream master was verified at `8f1084e3132a39270c3a13ebe37270a43ece2a01`.
For standard closed-loop evaluation, static inspection found no blocker requiring another core framework change.
Estimated core source files to modify: 0. A separate plugin package, configuration, dependency lock, and tests should suffice.
Real dependencies, GPU rendering, and policy effectiveness were unverified. Environments running older framework releases are outside this assessment.

## 1. Changes since the preceding audit

| Boundary | Current evidence | LIBERO integration |
| --- | --- | --- |
| Task parameters | runner._apply_request_overrides merges environment.parameters with case/request overrides | Pass suite/task_order_index/init_state_id/settle_steps and similar settings without changing Runner |
| Environment session | Runner creates a Session through registered Adapter.open(SessionSpec) | Wrap OffScreenRenderEnv in an independent Environment plugin |
| Action execution | Configuration selects Body and injects session/observation_updates/stop_executor | A new Body executes LIBERO actions through existing chunk/interrupt/feedback services |
| Action description | feedback.io.action declares format/dimensions | Use a plugin format such as libero_osc_pose_7d without changing the Action schema |
| Graph and results | Graph explicitly assembles Module, body.execute, env.observe, and outputs.success | Supply an evaluation Graph using native environment success |
| Batch | TaskAdapter plugins enumerate tasks and accept overrides by instance ID | No LIBERO branch in experiment.py is required |
| Execution/cache/display | Built-in process backend, observation cache, results, and video paths already exist | Reuse the existing infrastructure for standard integration |

The Xiaomi **Tool** requires the 12-dimensional RoboCasa365 interface and cannot directly control LIBERO.
The action-packing **Module** in `modules/xiaomi_robotics_1.py` reads format/dimensions from EnvironmentIO
without fixing the dimensionality, while `robocasa365_terminal.py` terminates using generic feedback and step counts.
A minimal integration can reuse both Modules if its Tool returns the expected vectors/format.
To avoid exposing a previous provider name in new configuration, a plugin rollout.py may register its own action and termination nodes.
Reusability of the Module does not establish compatibility of the Xiaomi checkpoint or Tool.

## 2. Proposed files

Recommended standalone package: `integrations/libero/`. The following files were proposed at the time of review.

| File relative to plugin package | Requirement | Purpose |
| --- | --- | --- |
| pyproject.toml | Required | Install robotenon_libero independently of core dependencies |
| src/robotenon_libero/__init__.py | Required | Lightweight import without environment connections or model loading |
| src/robotenon_libero/tasks.py | Required | Enumerate suite/task/trial with stable task IDs, task_order_index, initial state index, and horizon |
| src/robotenon_libero/environment.py | Required | Adapter/Session, path configuration, reset/set_init_state, observation and IO, simulation thread, video, shutdown |
| src/robotenon_libero/body.py | Required | Validate 7-dimensional actions, interpret controls, handle four-value step results, per-step feedback, and success/timeout |
| src/robotenon_libero/policy.py | Required for model control | Selected LIBERO policy Tool, image/state preprocessing, denormalization, action queue, end_session |
| src/robotenon_libero/rollout.py | Recommended | Action packing and termination nodes. Existing data-driven Modules can serve in the first version. |
| requirements-libero.lock | Required | Pin verified Python, simulator, and policy dependencies, plus upstream commit |
| scripts/bootstrap.py | Optional | Generate noninteractive LIBERO path configuration and check assets if not handled in environment.py |
| tests/test_contracts.py | Required | reset/step, IO, 7-dimensional actions, success/horizon, cached observations, close, and state isolation |
| tests/test_rollout.py | Required | Repeated fixed initial states, action replay, model baseline, sequential tasks, and process execution |

Add `configs/robotenon_libero.json` to the framework repository. One file can hold manifests for Environment/Body/Tool/Module/TaskAdapter/Graph.
The multi-file XR365 layout is optional. Import the package through plugin_environments/plugin_bodies/plugin_tools/plugin_modules/
plugin_task_adapters and add `configs/experiments/libero_parallel.json` if needed.

Existing runtime/runner.py, experiment.py, factory.py, workflow/*, services/*, schemas.py,
built-in registry manifests, and Xiaomi/RoboCasa plugins should require no changes.
Add a LIBERO user guide linked from docs/README.md and vendor-guide.md.

## 3. Technical work

1. **Dependency compatibility.** The upstream README recommends Python 3.8.13 and Torch 1.11. Its requirements pin
   robosuite 1.4.0, gym 0.25.2, and numpy 1.22.4. RoboTenon requires Python >=3.10.
   This discrepancy concerns the upstream reference installation. It does not establish that LIBERO requires Python 3.8.
   Test a separate Python 3.10 environment first and preserve the existing RoboCasa365 environment.
   `process` spawns the same interpreter and does not switch conda environments automatically.

2. **Initialization and benchmark protocol.** A seed does not replace official init_states.
   Record task_order_index, init_state_id, language, cameras, control frequency, horizon, and policy version.
   Upstream lifelong/metric.py applies 5 zero-valued 7-dimensional actions after set_init_state before counting evaluation steps.
   When reproducing a different model's evaluator, document its protocol and keep warm-up and step-count conventions consistent.

3. **Control semantics.** Defaults use Panda/OSC_POSE/20 Hz, external view, and wrist camera.
   Verify translation/rotation deltas, coordinate frames, gripper sign, clipping, image orientation, quaternion conversion, and normalization.
   Truncating a 12-dimensional action to 7 dimensions is insufficient to verify model adaptation.

4. **Success and step accounting.** LIBERO returns obs/reward/done/info. bddl_base_domain.step sets done from
   _check_success. Handle the step limit explicitly, distinguish success from timeout, and stop a chunk immediately on success.
   observe must retain final feedback, including any success already reported.

5. **Unattended rendering.** First import calls input when config.yaml is missing. Generate LIBERO_CONFIG_PATH configuration before startup.
   wrapper.reset retries indefinitely. The adapter needs bounded reset attempts or a process timeout.
   Create, step, render, and close on one simulation thread. Set EGL devices before import. Avoid nesting LIBERO's multiprocess vector environment.

6. **Suite names.** Start with libero_spatial/object/goal/90/10.
   This commit registered LIBERO_100 but omitted libero_100 from task_maps initialization. Enumeration does not establish constructibility.
   For all 100 tasks, combine 90/10 explicitly while preserving groups and order. Neither upstream nor core changes are needed.

## 4. If Python 3.8 must be retained

Add `bridge_server.py`, `remote_environment.py` (or a remote mode in environment.py),
startup configuration, and bridge tests on the plugin side. Body sends actions and Environment obtains observations through the plugin
using the same remote session. Transfer media through shared file references or a separate transport, avoiding ndarray in JSON.
A standalone 3.8 service cannot install the current RoboTenon release requiring >=3.10. It needs a lightweight bridge or a verified compatible service implementation.
Existing RPC hooks provide an integration point. A LIBERO server implementation still requires development.

## 5. Effort and acceptance

Assumptions: one developer familiar with this project and robosuite, an available Linux/NVIDIA environment, and working LIBERO policy/weights.
Estimates cover closed-loop evaluation integration. They exclude training, download waiting, and full evaluation runtime.

| Stage | Estimated person-days |
| --- | --- |
| Dependencies, assets, and offscreen rendering | 0.5–1 |
| TaskAdapter + Environment + Body | 1–2 |
| Policy Tool and Graph/node configuration | 1–2 |
| Fixed initial states, repeat/parallel runs, video/results, and regression checks | 1–2 |

Total: approximately **4–7 person-days**. A Python 3.8 external bridge would add **3–5 person-days**.
Transferring existing RoboCasa365 Xiaomi weights to LIBERO has performance and training costs that cannot be inferred from interface work.

Acceptance order: single-task reset/render/zero action → fixed-state action replay → known policy on one task → multiple initial states and tasks →
parallelism and failure cleanup. Verify the definitions of success and counted steps at each stage.
The rerun for environment parameters, external plugins, and Runner assembly reported 29 passed. The earlier 440-test full suite validated framework behavior
without establishing runnable LIBERO support.

## Source references

- Local: runner.py for parameter merging and SessionSpec assembly, factory.py for plugin imports and resource assembly,
  modules/xiaomi_robotics_1.py for IO-driven packing, and tools/xiaomi_robotics_1.py for the 12-dimensional restriction.
- [Upstream installation](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/README.md)
- [Dependencies](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/requirements.txt)
- [Environment wrapper](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/libero/envs/env_wrapper.py)
- [Success condition](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/libero/envs/bddl_base_domain.py)
- [Evaluation loop](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/lifelong/metric.py)
- [Task definitions and initial states](https://github.com/Lifelong-Robot-Learning/LIBERO/blob/8f1084e3132a39270c3a13ebe37270a43ece2a01/libero/libero/benchmark/__init__.py)
