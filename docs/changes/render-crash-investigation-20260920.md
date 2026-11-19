# AdjustToasterOvenTemperature crash investigation

[**English**](render-crash-investigation-20260920.md) · [简体中文](render-crash-investigation-20260920.zh-CN.md)

Experiment: `exp-20260920-110524-2de9629b`, task index 0, seed 7.

## Evidence

- Two attempts (`8cd25285-3c3c-46c5-aca7-cad20776e9a2` and
  `41d3aa6f-fa6d-4e5f-9bc4-9c5ccb538ae0`) terminated with SIGABRT on GPU 0.
  Both Python fault stacks stop at robosuite `binding_utils.py:174`,
  `mujoco.mjr_readPixels`.
- Each attempt completed 50 body-execution batches before the interrupted
  batch. The evaluator kept reporting 16 steps instead of accumulating them.
  The task's configured horizon is 450 steps, so the execution was already
  outside its intended evaluation budget before the crash.
- Versions: MuJoCo 3.3.1, robosuite 1.5.2, RoboCasa 1.0.1, PyOpenGL 3.1.10.
- GPU 0 replay: all 816 recorded actions from the second attempt ran in a
  separate process under GDB with the same seed and EGL backend. It exited
  normally. The replay did not run model inference, Portal or trace publishing.
  This does not reproduce the complete original execution environment and
  does not establish that the native rendering defect has been fixed.
- GPU 1 bounded replay: the same recorded actions stopped at exactly 450
  steps, with no environment success signal, and the renderer closed cleanly.
- NVIDIA reports zero volatile uncorrectable ECC errors on all eight GPUs.
  Kernel logs are inaccessible in this container, so driver Xid evidence
  could not be checked.

## Changes

- Terminal evaluation accumulates steps from its own previous published result,
  rather than the merged planner/verifier snapshots containing stale zeroes.
- Action generation reads the evaluator's authoritative counter and limits the
  final batch to the remaining horizon, including when a tool returns too many
  action vectors.
- Live progress reads the evaluator counter explicitly so later stale planner
  snapshots cannot reset the displayed crash progress to zero.
- Regression tests run the actual graph/workspace loop at horizons 34 and 450,
  with deliberately stale planner/verifier values and oversized tool outputs.

The existing process backend already restarts a lost worker and retries with
an isolated attempt directory. `max_retries=1` was honored in this experiment.
No retry limit, seed, success criterion, or installed simulator version changed.

## Deployment boundary

Source changes are synchronized to `/path/to/deployment`. Existing worker processes
already holding imported modules are not hot-patched. No active experiment was
stopped or restarted and no historical result was rewritten. Newly started
workers/runs load the fixes; an ongoing experiment is not a clean before/after
comparison if it replaces workers during execution.

## Remaining uncertainty

The exact native SIGABRT cause is not established. The confirmed horizon bug is
fixed, preventing this task from reaching the original out-of-budget crash
region. Fresh-process retry remains available for a native crash within budget.
Server diagnostic logs: `/tmp/robotenon-render-replay-gdb.log` and
`/tmp/robotenon-render-bounded.log`.
