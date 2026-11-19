# Experiment failure investigation and repairs (2026-09-19)

[**English**](incident-2026-09-19-worker-abort.md) · [简体中文](incident-2026-09-19-worker-abort.zh-CN.md)

## Historical results

| Experiment | Total tasks | Historical completed | Environment successes | Historical errors |
| --- | ---: | ---: | ---: | ---: |
| exp-20260919-003015-08bbd403 | 317 | 124 | 97 | 193 |
| exp-20260919-102712-2a54c875 | 317 | 139 | 59 | 178 |

Both runs processed all 317 tasks. For batch `status.json`, `completed` means scheduling ended.
`runner_cli.py` returns 1 when `num_errors > 0`. The Portal also marks a finished experiment containing errors as
`failed`. This state alone does not mean that all tasks failed or that the run was incomplete.

All 371 historical errors were `WorkerLost: worker connection closed`. Exit codes were checked individually.
The first run had 189 exits with -6 (SIGABRT) and 4 with -11 (SIGSEGV). The second had 177 with -6 and 1 with -11.
Of these faults, 369 parseable Worker stacks (192 from the first run and 177 from the second) pointed to the following path.

```text
robosuite/utils/binding_utils.py:174 in read_pixels
mujoco.mjr_readPixels(...)
... camera_rgb -> step -> concurrent.futures.thread
```

The processes crashed in simulation camera rendering. This failure differs from a normal policy task failure.
The historical `env_steps=0` value was a hardcoded exception-path placeholder. For example, in the second run
the OpenToasterOvenDoor live file recorded 112 steps before the Worker was lost in body_execute.

GPU 6 completed 53 tasks in one run and 49 in the other, without error. The other 7 Worker GPUs
accounted for all 371 errors. The correlation with device or startup conditions does not establish
a damaged GPU or an internal driver cause. Logs did not establish CUDA OOM.

In the second run, 60 historical completed records had reason `continue`. Of these, 59
had a planner stop, an empty remaining list, and a visual verifier reporting subtask success, while the environment
had not confirmed success. The other case, AssembleCookingArray, actually had a failed Graph. Qwen reached its
1024-token limit and truncated the JSON. Another 20 tasks reached the horizon.

In the first run, 25 other tasks reached the horizon, and CoolBakedCake and StartElectricKettle failed to read
observation-cache URIs (Graph failure). The previous evaluator counted returned Graph failures
as completed. These three execution failures were therefore missing from the historical error count. The table retains the original definitions.

## Code changes

1. `runtime/parallel.py` temporarily sets CUDA/EGL devices and thread environment variables before spawning the child,
   then restores the parent environment. Server sitecustomize imports Transformers/
   Torch before the Worker entry point, so setting devices only in `_worker_main` is too late for reliable initialization.
   This hardening alone does not explain all historical native crashes.
2. WorkerLost includes the signal name, Worker fault log path, and first crash stack frame.
3. On failure, recover the last known step from the live file in the same run, set `env_steps_partial=true`,
   and retain the active node and progress time. Ignore other runs' stale progress and distinguish partial from exact final counts.
4. The RoboCasa configuration enables `policy.planner_stop_requires_environment_success`. Until the environment confirms
   success, reject planner stop and continue with the original instruction. Other environments retain their defaults.
   Record accepted planner stops explicitly as `reason=planner_stop`.
5. The 8-GPU configuration defaults to `max_retries=1`: retry a lost Worker once with a new Worker and attempt.
   Preserve the original lost attempt. Completing normally without reaching the goal does not trigger this retry.
6. Record a returned failed/error Graph as an execution failure on both task and attempt,
   retaining `reason=graph_error` and the actual error message.

Historical experiment files and results were preserved. A reused full experiment_override.json containing explicit
max_retries=0 overrides the new default. Create a new experiment or update the override explicitly.

## Verification and scope

- Local and server full pytest suites each reported 237 passed (12.53 seconds locally and 59.91 seconds on server).
- Simulation rendering across 7 GPUs completed 8,400 steps. A separate 1,800-step single-GPU check also passed.
- Exact replay of 48 historical StirVegetables/seed=299 actions passed.
- A complete OpenToasterOvenDoor/seed=33 episode achieved environment success after 117 steps.
- The original 8-Worker diagnostic configuration `eight-worker-native2` ran 8 historically failing tasks at
  horizon=160 with 0 errors. No native SIGABRT stack was reproduced.
- After repair, `postfix-eight-smoke` completed the same 8 tasks at their 160-step horizon, totaling
  1,280 steps and 0 errors. There were 0 task successes. This short run tested execution stability.
- SHA-256 checks matched for 221 source, configuration, test, and documentation files across robotenon, tests, configs, scripts, docs, and examples
  on both hosts. The comparison excluded run data, caches, and installed system dependencies.

These checks do not establish that the renderer crash was eliminated or substitute for the original full 317-task evaluation.
Device binding and one Worker retry reduce exposure and retain diagnostics. Recurring SIGABRT/SIGSEGV
requires investigation of the rendering driver and resource lifecycle using new fault logs.

Server-side historical evidence in `/path/to/deployment/data/xr365/portal-runs-8gpu/<experiment-id>/` includes
summary.json, task .live files, and workers/*/worker-fault.log.
Diagnostic artifacts are in `/path/to/deployment/data/xr365/diagnostic-20260919/`.
Before synchronization, modified files were backed up in `/path/to/deployment/.xr365-sync/fix-20260919-235712/`.

## Follow-up repairs on 2026-09-20

### Cache read race

`read_uri_bytes` read SQLite before `_pending`. If the persistence thread committed to SQLite and removed
`_pending` between the two reads, both could miss and report `FileNotFoundError(cache://...)` incorrectly.
After an in-memory miss, the fix rechecks the committed disk index. A duplicate hot-ring scan,
which could not close the race, was removed.

The new test schedules the first disk miss, persistence commit and pending removal, then the memory read using events.
It failed consistently against the original read method and passed after repair. This confirms the code race.
Neither historical log contained thread timing sufficient to attribute its failure individually to this window.

### Truncated model responses

The Qwen Tool checks `finish_reason=length` and rejects truncated output. By default it makes up to three requests,
increasing the token budget from 1024 to 2048 and 4096 on truncation, then reports an execution error if exhausted.
Invalid JSON also uses bounded retries, which `max_response_retries=0` disables. Network unavailability still fails immediately.
JSON extraction parses only the first object so a valid nested object inside an unclosed outer object cannot pass as a complete response.

The four pre-change files were backed up on the server at
`/path/to/deployment/.xr365-sync/followup-20260920/before.tar.gz`. The new test file did not previously exist.
Native rendering retained the earlier device initialization, thread binding, resource cleanup, and Worker retry fixes.
No speculative changes were made to MuJoCo or GPU drivers.


### Follow-up verification

- Full local suite: 243 passed in 13.01 seconds, using the snapshot preceding concurrent Graph changes.
- Server full suite: 242 passed. One deployment test reached its 3-second startup timeout during concurrent 8-GPU model loading.
  Rerunning the model deployment file in isolation passed all 11 tests in 25.43 seconds.
- Server cache and Qwen response subset: all 13 passed in 0.31 seconds.
- Real-model 8-GPU regression `followup-eight-600` completed 8 tasks and 4,298 steps with
  0 errors. All 8 Worker fault logs were empty and there were no retries.
  PickPlaceCounterToToasterOven succeeded after 205 steps and PickPlaceCounterToMicrowave after 493. The remaining tasks,
  CoolBakedCake、StartElectricKettle、AssembleCookingArray、AddLemonToFish、
  StirVegetables, and StoreLeftoversByType reached the 600-step horizon normally.
- This run used the historical task seeds, real model, and diagnostic configuration snapshot from startup. No Graph files changed.
  Server results are in `data/xr365/diagnostic-20260920/followup-eight-600/`.
- Synchronization covered 5 files: cache implementation, Qwen Tool, their two test files, and this document.
  SHA-256 matched file by file. Concurrent Graph work was not overwritten or rolled back.

The regression did not reproduce the native rendering crash, but the result neither resolves its underlying cause
nor replaces a complete 317-task evaluation. Historical results were retained.
