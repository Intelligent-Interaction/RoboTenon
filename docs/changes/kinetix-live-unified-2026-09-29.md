# Kinetix realtime through the unified evaluator

[**English**](kinetix-live-unified-2026-09-29.md) · [简体中文](kinetix-live-unified-2026-09-29.zh-CN.md)

## Implemented

`robotenon_benchmark_support.evaluate --benchmark kinetix --profile realtime`
uses the same backend loader, model processes, batching, episode queue and result
aggregation as sync profiles. The generated live Graph adds an explicit
`live_tick` between observation and termination. Physics remains in Body; no
background task steps the simulator.

The live Tool queues each observation in FIFO order and processes model replies
asynchronously. History comes from backend SPEC; chunks are consumed as model
messages without a new forward for every cached action. Each simulator tick uses
the newest delivered action, or `benchmark.get_hold_action(last_fresh_action)`.
Kinetix repeats its last fresh command, starting from zeros. Default first-tick
behavior does not wait for model inference; `wait_first_action=true` waits up to
30 seconds. Native terminal signals stop action execution and new observations
immediately. In-flight inference is drained only to close the session correctly.

Clock semantics follow the pinned harness's LiveEpisodeRunner, ActionBuffer and
Clock: setup is excluded, each tick waits its remaining period divided by pace,
there are no catch-up bursts after a slow step, and no pacing after native done.
A runner horizon still sends its final non-native-terminal observation and paces,
as upstream does. Real-time counters and elapsed wall time are returned under
`outputs.rt_metrics`; ordinary seed, horizon and success statistics are preserved.
Model-specific continuous inference and latency-aware action selection are not
implicitly enabled.

## Validation

- Full repository regression: **946 passed, 2 skipped** before the final additional
  error-propagation check. Skips are local torch absence and a Linux-only test.
  The final focused live/entry/backend suite passed **53 checks**.
- 20 benchmark entries / **157 profiles** now generate and load, including live.
- Slow-model integration tests run real model-service and simulator processes:
  150 ms inference versus 50 Hz environment ticks, both first-action modes, two
  sessions, hold behavior, six-step terminal, metrics and clean shutdown.
- A background inference error remains an error even if termination races its
  delivery; it is not silently counted as a successful evaluation.
- Real Kinetix CPU smoke: **2 episodes × 20 steps, zero runtime errors**, two actual
  model batches of size 2. Symbolic observations, Cartpole Thrust, seed 0, action
  noise 0.1, target 10 Hz, pace 1. See [raw report](evidence/kinetix-live/smoke.json).
- CPU JAX compilation made the effective control rate approximately **2.1 Hz**.
  This is reported, not hidden or represented as meeting the target frequency.
- Both fake-policy episodes failed the task. This smoke verifies runtime behavior,
  not model accuracy or full RTC benchmark results.

No server code was replaced and the running 8-GPU RoboCasa365 full evaluation was
not restarted or used for these tests. The real smoke used an existing local CPU
Kinetix environment and existing RTC assets.

## Other benchmark audit fixes

Compatibility commands for LIBERO-Plus, MolmoSpaces, RoboMME and Simpler now retain
the default profiles declared in the catalog. Invalid protocol values are rejected
before model loading. The low-level `configure` CLI directs live users to the
unified evaluator, which supplies the required backend SPEC and asynchronous Tool.
