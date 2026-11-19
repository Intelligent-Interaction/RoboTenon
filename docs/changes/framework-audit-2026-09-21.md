# Framework audit of usability, configuration syntax, and performance

[**English**](framework-audit-2026-09-21.md) · [简体中文](framework-audit-2026-09-21.zh-CN.md)

Date: 2026-09-21. The workspace snapshot included earlier uncommitted fixes. The audit inspected configuration, CLI, preflight, Graph, experiment management, and observability queries. Eight sets of reproductions used temporary data. No framework implementation changed.

This is a pre-repair snapshot. See the [repair record](framework-audit-repairs-2026-09-21.md) for subsequent changes addressing A01–A08. All reproduction and test counts below describe the earlier version.

Priorities were early configuration errors, consistent preflight and execution behavior, and lower cost from repeated Graph result processing. Historical data paths also contained cases missed by the previous repairs.

## Findings and priorities

| ID | Priority | Finding | Affected use |
| --- | --- | --- | --- |
| A01 | High | Unknown fields accepted, allowing misspellings to fall back silently to defaults | Authoring Graphs, disabling Tools, changing step limits |
| A02 | High | Batch CLI accepted and discarded `--graph-id` | Switching from single runs to batch or changing workflows |
| A03 | High | Preflight and execution used different validation | Batch overrides, model ports, output paths |
| A04 | High | An oversized legacy summary blocked queries before automatic index recovery | Upgrading legacy experiments or recovering missing indices |
| A05 | High-frequency workflows first | Large unused node results significantly increased Graph scheduling cost | Frequent loops, larger plans, structured observations |
| A06 | Medium | History decoding processed the whole window before sampling | Long replays, regularly spaced observations |
| A07 | Medium | Replacing a Metrics file could retain the old offset | Rotation, file replacement, long-running services |
| A08 | When the legacy API is used | The `/api/runs` limit did not bound scanning | Many historical manifests, scripts using the old API |

## Confirmed behavior and recommendations

### A01: Configuration misspellings survive parsing

Configuration and Graph models inherited `RoboModel` with `extra="allow"`. Some route/loop substructures checked unknown fields independently, but there was no common strict configuration boundary.

The parser accepted `max_step: 2` while actual `max_steps` remained 1000, accepted `inptus` while `inputs` remained null, and accepted `enabeld: false` while the Tool remained enabled. Successful loading gave no indication that these settings had been ignored.

Introduce strict framework configuration models. Normalize legacy aliases first. Report the JSON path for unknown fields and suggest close matches. Keep plugin parameters extensible inside declared fields such as `params`.

Sources: [schemas.py](../../robotenon/schemas.py), [configuration models](../../robotenon/runtime/config.py), and [Graph definitions](../../robotenon/workflow/definition.py). Reproduction key: `config_typos`.

### A02: The same CLI argument behaves differently in single and batch mode

The parser accepted `--batch --graph-id alternate`. Single-task and preflight requests retained graph_id, but batch requests built by `experiment_request_from_args` omitted it. Batch execution then selected the Graph from configuration. The override could be ignored and preflight could validate a different Graph from the one run.

Another entry-point difference was reproduced: `robosys web --max-queries 4` rejected an argument accepted by the parsers for `robosys-web` / `python -m robosys.web.server`.

Unify request structures and argument declarations. Specify single, batch, and shared options, reject unsupported combinations at parse time, and persist supported overrides in the effective configuration and snapshot. Reuse the parser for the Web entry point.

Sources: [runner_cli.py](../../robotenon/runtime/runner_cli.py), [batch entry](../../robotenon/runtime/experiment.py), and [common CLI](../../robotenon/cli.py). Reproduction key: `cli_mode`. Verification stopped at request construction without launching an environment.

### A03: Preflight passes known-invalid requests

Three cases were reproduced.

- Override for a missing TaskAdapter: `--preflight --batch` returned ok with exit code 0, while the batch entry immediately reported `unknown task adapter override targets`.
- Model port 70000: a known range error was classified as deferred and the overall check returned ok.
- An existing ancestor of the output path was a regular file. Preflight checked writability and reported ok.

Reuse pure configuration resolution and constraints from execution without initializing models or simulators. Build the same effective single/batch request, then check parameter targets, path types, port ranges, Graphs, and component references. Distinguish invalid input, an unavailable external resource, and a check that cannot currently run.

Sources: [preflight entry](../../robotenon/runtime/runner_cli.py) and [preflight.py](../../robotenon/runtime/preflight.py). Reproduction key: `preflight`.

### A04: Legacy summary size limit blocks automatic recovery

A finished experiment had released resources, a run lock, valid per-task JSON, an absent episode index, and a legacy summary larger than 16 MiB. `_refresh_job` read the summary first and failed the size check before `_queue_index_recovery` ran. The recovery queue stayed empty and `index_recovery` was unset.

This caused detail queries to fail and could break a list page containing that experiment. The maintenance CLI provided a manual path, while automatic recovery missed this case.

Identify missing or dirty indices and schedule recovery first. Return minimal metadata with an explicit status while it runs. Record an oversized summary as an experiment-specific diagnostic so the list remains usable.

Source: [legacy summary reader and recovery scheduling](../../robotenon/web/experiments.py), especially lines 710–715 and 799. Reproduction key: `oversized_summary`.

### A05: Graph repeatedly snapshots unused results

A producer and a loop node ran with checkpointing disabled and no model, environment, or disk writes. The loop declared empty inputs and read only its own iteration for 40 executions. The producer returned an additional unused integer list. The table reports medians across three paired samples.

| Upstream output | Producer and 40 loop iterations | p95 interval between loop starts |
| --- | ---: | ---: |
| Empty extra list | 12.51 ms | 0.58 ms |
| 10000 integers, 58890 JSON bytes | 705.55 ms | 21.72 ms |

Each iteration serialized outputs and froze all records, then constructed another frozen view at batch start. Some snapshot work occurred even without ready nodes. Cost grew with historical output size. These local synthetic measurements do not estimate hardware action latency or guarantee a specific speedup.

Reuse immutable, versioned published results, share unchanged records, cache frozen views, and skip snapshot construction when no node is ready. Prefer Artifact/ObservationRef for large arrays. Preserve parallel isolation, within-batch snapshot reads, and checkpoint consistency.

Sources: [scheduler.py](../../robotenon/workflow/scheduler.py), [workspace.py](../../robotenon/contracts/workspace.py), and [node publication](../../robotenon/workflow/runtime.py). Reproduction key: `graph_payload`.

### A06: Observation history reads length × interval entries

To return 50 samples at interval 100, the implementation read and decoded 5000 JSON records before sampling in Python. Both length and interval could reach 1000, so one request could process a million records. The 4 MiB response limit applied only afterwards.

Move sampling and pagination into the database, bound rows scanned, decoded bytes, and query time, and return explicit truncation and continuation metadata.

Sources: [observation_history](../../robotenon/web/query.py) and [HTTP parameter limits](../../robotenon/web/server.py). Reproduction key: `observation_window`. The test used 5000 rows and did not generate the million-row case.

### A07: Metrics rotation detection uses file size alone

`reload` reset the offset only when the file was smaller than that offset. Replacing a file containing gauge=1 with a distinct, equal-length file containing gauge=2 still returned 1. A larger replacement could likewise skip its prefix.

Record the device/inode or generation of the open file and rebuild read state on identity change. Test replacement, truncation, and partially written tails separately.

Source: [MetricsDaemon.reload](../../robotenon/infra/metrics.py). Reproduction key: `metrics_rotation`.

### A08: Legacy run listing scans all manifests

`PortalData.runs(limit=1)` read all 30 manifests, then sorted them to return 1. Cost grew with directory size. This finding concerned legacy `/api/runs`. The newer `/api/experiments` already used the SQLite catalog introduced previously.

If the interface remains supported, index its manifests and use common query budgets. If retained for compatibility, document deprecation and migration. A return limit alone does not bound scan cost.

Source: [PortalData.runs](../../robotenon/web/query.py). Reproduction key: `legacy_run_scan`.

## Medium-term usability and syntax proposals

The following proposals had no measured performance claims.

1. **Configuration assistance.** Export JSON Schema for editor completion. Show the effective configuration, including defaults and whether each value came from main configuration, an experiment override, or CLI.
2. **Graph authoring.** Provide a checker and formatter for reciprocal next/predecessors declarations, missing bindings, unused parameters, and unreachable branches. Preserve existing next/goto/loop semantics while reducing duplicate structural maintenance.
3. **Common entry points.** Align run/check/inspect and share request resolution. Include field paths, error codes, and next steps. Distinguish recovering indices, 503 retry waits, and failures in the Portal. Read-only clients can honor Retry-After.
4. **Maintenance boundaries.** Separate experiment lifecycle, historical queries, and result recovery from the large manager incrementally, with existing behavior and fault tests defining the interfaces.
5. **Continuous acceptance.** Convert the boundary cases to tests. Cover supported Python/Pydantic versions and measure long-running multi-Worker behavior, memory/RSS/disk growth, and action tail latency on target GPUs before promising workload gains.

Suggested order: A01/A02/A03 → A04 → A05 → A06/A07. Schedule A08 according to actual legacy API use. Four deferred items from the previous audit (action ledger, Planner validation, GPT cancellation retries, and stale Trace responses) were outside these 8 findings and unchanged.

## Verification and reproduction

- Full suite: **526 passed, 1 skipped, 1 existing warning**, in 26.70 seconds. See [pytest.log](framework-audit-2026-09-21/pytest.log). The existing tests did not cover every finding.
- All 8 reproductions completed. [results.json](framework-audit-2026-09-21/results.json) contains raw results, timings, and source fingerprints. The script asserts pre-repair behavior and needs different assertions for repaired regressions.
- Environment: macOS ARM64 and Python 3.11.10. Reproduction output went to temporary directories. SHA-256 hashes of framework, configuration, scripts, and tests matched before and after.
- No browser acceptance, real model/GPU/simulator/robot run, or long-duration load test was performed. Portal findings came from server-side paths and temporary data. Usability proposals were not tested in user studies.

```bash
python docs/changes/framework-audit-2026-09-21/reproduce.py \
  --output /tmp/robotenon-framework-audit.json
python -m pytest -o addopts= -q
```

Audit artifacts: [reproduction script](framework-audit-2026-09-21/reproduce.py), [execution log](framework-audit-2026-09-21/reproduce.log), and [verification metadata](framework-audit-2026-09-21/verification.json).
