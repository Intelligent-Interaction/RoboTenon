# Bounded live status snapshots

[**English**](bounded-live-status-2026-09-29.md) · [简体中文](bounded-live-status-2026-09-29.zh-CN.md)

`robotenon/runtime/runner.py` now retains at most 256 recent timeline spans and 256 recent events per episode. Normal running snapshots are written no more than once per second. Starting, terminal and error snapshots bypass throttling. Span IDs use a monotonic sequence rather than the retained-window length. `timeline_window` exposes the limit, total span count and whether older records were dropped.

The change only bounds the live monitoring snapshot. It does not alter graph execution, observations, model history, action chunks, environment steps or final evaluation statistics. Full tracing, when separately enabled, retains its existing mechanism; the live file itself is no longer a complete historical timeline.

Read-only serialization probe on the active server's episode snapshot:

| Snapshot | Spans | JSON bytes | Median JSON serialization, 3 trials |
| --- | ---: | ---: | ---: |
| Original | 9335 | 5765523 | 0.154733 s |
| Recent window | 256 | 251041 | 0.006140 s |

This is about 25× less serialization time for this snapshot, not a measured end-to-end simulator speedup. Per-second write throttling is additional to this reduction. No running simulation process was modified or restarted during the probe.

Regression tests exercise 300-node successful and failed graphs, assert all nodes execute, final outputs remain correct, retained spans are bounded and have unique IDs, writes are throttled, and terminal/error states are flushed immediately.

Already-running Python workers retain the old imported implementation. Installing the patch on disk alone cannot accelerate them. Apply it with a controlled process restart; preserve existing evaluation artifacts and explicitly handle unfinished episodes. The unified CLI has no automatic resume flag.
