# Recovering from native renderer crashes

[**English**](render-crash-recovery-20260920.md) · [简体中文](render-crash-recovery-20260920.zh-CN.md)

## Confirmed observations

In experiment `exp-20260920-150610-172b0aad`, Workers received SIGABRT on AddIceCubes, AddLemonToFish, AddMarshmallow, AddSweetener, AdjustHeat, and AfterwashSorting. Python native fault logs placed the current thread in robosuite `binding_utils.py:174 / read_pixels`, reached through action execution, environment step, camera observation, and MuJoCo pixel readback. The last persisted environment feedback had no success signal. These cases remain unsuccessful.

Environment creation, reset, step, and close already ran on one dedicated thread. Evidence did not isolate a particular NVIDIA driver defect, simple thread migration, or the earlier outputs accounting issue as the cause. The previous recovery restarted the same EGL backend and repeated faults could exhaust retries.

## Recovery

Configure the following in `runtime.execution`.

```json
{
  "max_retries": 1,
  "render_retry_backend": "osmesa"
}
```

The 8-GPU configuration enables this option by default. Other configurations retain their settings. The first attempt uses the existing renderer. Only Worker loss from SIGABRT/SIGSEGV with a fault log pointing to robosuite read_pixels selects OSMesa for the next permitted retry. Model errors, timeouts, and other native crashes do not trigger fallback. max_retries still applies, including 0.

Set MUJOCO_GL/PYOPENGL_PLATFORM before creating the software-rendering process and preload system libstdc++ when needed. This avoids losing communication pipes through an exec inside an active multiprocessing Worker. worker_threads limits Mesa threads. Models remain on the original GPU, with the same task, seed, horizon, image size, and success definition.

Recovery starts a fresh attempt from the same seed. attempt/status.json retains the crash error, retry_render_backend, and actual next render_backend. The software-rendering Worker closes after that case succeeds or fails. Other tasks retain their default renderer. Software rendering adds CPU cost.

## Activation

Synchronizing source and configuration does not hot-replace a running Coordinator. A new Coordinator, including one restarted under the original experiment ID after stopping, uses the new retry mechanism. Later results from the old process cannot validate the new code.

## Verification

Regression tests cover native crash classification, environment setup/restoration before spawn, backend switching, retry budgets, and reporting the actual task result after recovery. A Linux integration uses real os.abort to verify a new attempt/Worker starts with software rendering after process loss.

Server verification used AddIceCubes, seed 72, 256×256 cameras, and actions from the failed experiment. The diagnostic script/logs were /tmp/robotenon-render-backend-check.py, /tmp/robotenon-osmesa-check.log, and /tmp/robotenon-egl-check.log. GPU and software comparisons ran without models or changes to existing experiment results. After the recorded sequence, the last action was repeated solely for an extended rendering stability check.

This verifies a recovery path without establishing a fix for the underlying NVIDIA/MuJoCo defect.


## Verification and deployment record

- Full local suite: 394 passed, 1 Linux-only native abort integration skipped. Full server suite: 395 passed.
- OSMesa, AddIceCubes/seed 72, three 256×256 cameras: 1000 steps in about 539 seconds, with normal shutdown. The first 816 steps replayed the failing experiment, then the last action repeated to extend the test.
- EGL independently completed the same 1000-step replay. The trial neither reproduced the original crash nor established a deterministic failure at step 800.
- Restarted the experiment under the same ID with a new Coordinator (PID 3234028). The 67 completed tasks remained byte-identical by SHA256. Rescheduled 250 non-completed tasks, including error/running/pending. Active tasks restarted from their original seeds. New status.config_hash matched the current fallback-renderer configuration.
- Pre-restart state and completed-result hash backup: `/path/to/deployment/render-restart-20260920-164058`. Source backup before deployment: `/path/to/deployment/render-retry-20260920-163551.tar`.

- OSMesa image checks: all three cameras returned 256×256×3 uint8 images with pixel standard deviations around 67–68, ruling out black or constant frames.

- Under GDB, a full Graph with real simulation/cache/video, recorded actions, and a fixed planner completed 1100 steps at horizon. It did not run the original model inference, so the reproduction cannot rule out native faults under production conditions. Log: `/tmp/robotenon-workflow-render-gdb2.log`.
