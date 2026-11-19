# Framework audit repairs

[**English**](framework-audit-repairs-2026-09-21.md) · [简体中文](framework-audit-repairs-2026-09-21.zh-CN.md)

Date: 2026-09-21. This record documents the fixes for A01–A08 in the [framework usability, syntax, and performance audit](framework-audit-2026-09-21.md). The earlier audit describes the pre-repair snapshot.

## Changes

| ID | Repair |
| --- | --- |
| A01 | Strict framework and Graph configuration models reject unknown fields at load time. Plugins keep their own values in `params` or `metadata`. Project the main configuration onto the appropriate manifest before parsing it to exclude unrelated model/runtime settings. Normalize legacy aliases for environment, monitors, and observability persistence first. |
| A02 | Batch requests retain `graph_id`. Graph selection, episodes, Worker profiles, and frozen configuration snapshots all use that Graph. |
| A03 | CLI preflight loads the effective batch or single request. It checks adapter override targets and enabled adapters, model port ranges, existing nondirectory ancestors of output paths, and single-task output file targets. |
| A04 | Oversized legacy `summary.json` produces a per-job `summary_error` diagnostic. Index recovery can continue without interrupting job detail or list queries. |
| A05 | The Graph scheduler reuses immutable frozen values keyed by published result objects. With no ready nodes it returns without constructing a snapshot. Activations still have independent outer snapshots and execution barriers. |
| A06 | SQLite window functions select spaced observation history samples. Only returned items are decoded. Cap `length × interval` at 100,000 and return HTTP 400 with a reason for excessive requests. |
| A07 | Metrics WAL reload recognizes replacements using the device and inode of the open file descriptor. On replacement or truncation it rebuilds read state from the start. |
| A08 | `/api/runs` uses a persistent SQLite manifest index. First access backfills legacy entries, then database queries use `limit` to select recent records. Recorder registers writes/updates in the index. The legacy scan remains an availability fallback when the index is unavailable. |

## Follow-up repairs

Further review identified these cases.

- Check main configuration and submanifests for unknown fields before normalization, merging, and projection so misspelled top-level keys cannot disappear. Extension parameters belong in `parameters`, `params`, or `metadata`.
- Batch preflight and execution now share Task Adapter plugin loading, override merging, construction, and side-effect-free parameter validation. Invalid trial counts fail preflight.
- Run index queries prune deleted manifests. A failed registration marks the index dirty for reconciliation on the next successful query. Queue SQLite files are indexed as well, so `/api/runs` no longer discovers and reads every queue database recursively.
- Observation history limits are 1,000 returned entries, a 100,000-record scan window, a 0.5-second query budget, and a 3 MiB decode budget.

Strict validation applies to framework-defined configuration models. Custom Modules, Tools, Environments, Monitors, and adapters retain explicit extension parameter fields. Graph nodes use `metadata` for custom metadata. Misspelled framework fields now require correction before loading succeeds.

Initial legacy-run backfill scans the history directory once. Subsequent Portal queries use SQLite limit. Manifests created or updated by RoboTenon `EpisodeRecorder` enter the index synchronously. Copying files directly into the directory bypasses this hook. Rebuild `.robotenon-run-index.sqlite3` for another backfill. If the index directory is unwritable or SQLite unavailable, Portal falls back to scanning.

## Verification scope

Follow-up tests cover configuration boundaries, batch preflight, Run index recovery, the queue catalog, and observation query budgets. The full suite reported 531 passed and 1 skipped. No models, GPUs, simulators, or real experiments were started. Measure Graph scheduler gains on the target deployment under load.
