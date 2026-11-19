# Experiment queueing, call duration, and failure causes (2026-09-20)

[**English**](incident-2026-09-20-queue-and-timing.md) · [简体中文](incident-2026-09-20-queue-and-timing.zh-CN.md)

## Observed evidence

In the snapshot, `exp-20260920-104320-ca107d28` had finished 25/317 cases, with 16 successes, 9 failures, and an interim 36% failure rate. It was still running. All 9 failed cases had a Body deadline signal in attempt/resources/safety.sqlite3.

Effective settings were action_timeout_ms=120000, chunk_duration_ms=16000, and action_chunk_size=16. Body uses the earliest action, chunk, and absolute deadline. CloseMicrowave body_execute ran from 02:47:34.127896 UTC to 02:47:50.097815 UTC, about 15.97 seconds, and exhausted the chunk budget. The old message always said action timeout after 120000 ms. A safety stop can precede the next evaluator call, so old summaries with reason=continue and env_steps=0/16 may reflect the previous evaluation.

Trace routing:plan/action spans were route-condition checks. One routing:plan lasted 0.366 ms and incorrectly appeared as 0 ms on the call-duration chart because of browser date precision and integer formatting. Actual plan/node and Tool spans took seconds.

Historical experiment exp-20260920-095212-e1b99275 failed startup with the legacy Graph validation error ValueError: edge reset->plan requires source goto or default. No GPU memory conflict was established. The submitter did lack a waiting queue and immediately started every submission.

## Changes

- Portal persists waiting state and schedules experiments serially. It dispatches the next job in the background, recovers live PIDs on restart, and supports cancellation of waiting jobs and global stop. Each experiment retains its internal 8-GPU parallelism.
- The overview reports a separate waiting count and the list offers waiting-job cancellation.
- Exclude routing spans from the call-duration chart while retaining raw Trace and routing diagnostics. Keep submillisecond precision, display durations below 1 ms as <1 ms, and use square-ended bars.
- XR365's 8-GPU default chunk budget is 1920000 ms (16 × 120000 ms). Both action and chunk safety deadlines remain.
- Body distinguishes chunk, single-action, and absolute timeouts. A safety_stop summary no longer inherits continue.

## Verification and deployment

Local and model-runtime server environments each passed 55 Python regressions, covering concurrent submission, waiting-job cancellation, restart recovery, background dispatch, sequential starts using real subprocesses, and timeout classification. All 9 JavaScript tests passed.

Pre-sync backup: /path/to/deployment/queue-fix-20260920-110044.tar. Only the Portal was restarted. Other processes continued running.

Existing experiments retain their loaded budgets. Newly created experiments use the new default. Fork/rerun retains historical parameters by design. When forking an older experiment, set policy.chunk_duration_ms to 1920000. No full GPU evaluation established a post-repair success rate, and the running historical experiment did not test the new budget.
