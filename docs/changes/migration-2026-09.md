# Architecture repairs and migration notes for September 2026

[**English**](migration-2026-09.md) · [简体中文](migration-2026-09.zh-CN.md)

## 2026-09-23 — OR groups in predecessors

- `predecessors` accepts two levels of grouping, with AND between outer groups and OR within each group. `["a", ["b", "c"]]` means `a AND (b OR c)`.
- A flat list remains a strict AND and requires no migration. Loading rejects empty groups, three-level nesting, duplicate members, and unknown nodes.
- The compiler checks all group members as dependency edges for references and cycles. Checkpoints retain the members that actually arrived. Other members of an already satisfied OR group disappear from waiting diagnostics.
- `goto` continues to bypass the predecessor join. Conversion of legacy `edges` generates flat AND predecessors only.

## 2026-09-20 — Integration and infrastructure plugins

- Added environment.parameters, TaskCase.parameters.environment_parameters, and EpisodeRequest.environment_parameters. Objects merge recursively, lists replace, and canonical fields take final precedence.
- ExperimentRequest accepts task_adapter_overrides/environment_parameters, with corresponding JSON CLI arguments. split is no longer restricted to pretrain/target.
- TaskAdapter constructor overrides merge recursively. Configurations relying on shallow replacement should review retained nested defaults. Providers declare accepted historical shortcuts through cli_overrides.
- Backends use a registry and capability-based assembly for single and batch execution. Existing in_process/process configurations remain valid.
- Register safety, checkpoint, action deduplication, and observation stores. require_shared verifies the shared capability. The remote observation backend creates an actual remote client and requires params.socket_path. For a local cache previously labeled remote, select hybrid.
- RPC clients and servers support transport plugins as well as the original UDS constructor. Both Body connections inherit transport configuration.
- ObservationFrame.modality/producer and ObservationChannel.modality accept nonempty strings and preserve explicitly declared modalities. role, capture_policy, and safety contracts continue to apply.
- Application assembly now handles cleanup consistently on failure. Application-owned context/safety stores close with the application. Injected objects retain external ownership.

See [infrastructure plugins](../infrastructure-plugins.md) for configuration and aliases and the [verification record](verification-2026-09.md) for test coverage.


## 2026-09-20 — Separate next, goto, and loop syntax

- Convert ordinary legacy goto to next. Keep the target's predecessors.
- Convert a legacy loop targeting an arbitrary node, including kind: loop, to goto and remove kind.
- New loop repeats the current node only. Accepts true or {"when": "condition"}, without node or kind.
- Ordinary legacy edges remain supported. Migrate back edges with kind to a node goto and ordinary edges to next/predecessors.
- default retains ordinary successor behavior and applies only when none of the three operations match.
- An actual predecessor next invalidates an old self-loop invocation. Normal batches and self-loops are scheduled independently. ctx uses read snapshots and private workspaces. Invalidation and commit are serialized.
- The Graph fingerprint includes routing_version=5. Old in-flight checkpoints require explicit restart under the new semantics.
- configs/graphs.json and configs/robotenon_xr365_graphs.json were migrated. Migrate externally saved full experiment configurations as well.

See [Workflow](../workflow.md) and [node-private contexts](../node-private-context.md).


## 2026-09-16 — Core architecture repairs

- Fixed P3/P4 latch overwrite for same-named signals. In-memory and SQLite storage use the same merge rules.
- Fixed stale completion records in multi-node loops, false recursion in parallel subgraphs, and in-flight checkpoint resumption.
- Body RPC separates execution and control connections. Total deadlines include lock wait. Cancellation cleans the connection and requests a remote stop.
- edges and predecessors/goto share WaveScheduler.
- Separated contracts, storage, compiler, conditions, scheduler, and operations.
- Added NodeResult/GraphModule, explicit patch-conflict checks, configuration preflight, and checkpoint version/fingerprints.
- Service requests now use ServiceContext, removing server-side imports of workflow implementation.
- Added lazy exports and deferred assembly, Module cancellation rollback, service startup-failure cleanup, and closure of short-lived SQLite connections.
- One strict JSON codec handles checkpoint/ctx/parameter hashes and IPC. It rejects unknown objects and NaN/Infinity instead of silently stringifying them. The Monitor observation wrapper safely restores temporary singleton bindings.
- Separated Web static assets, query layer, and Grafana launcher. Included static assets in the wheel.
- Added fault regressions, dependency boundary checks, and CI configuration.
- Restored a separate resident ToolRuntime for models, Memory, Skill, and custom capabilities. Added `tools_configs.json`, ToolRegistry, and `ctx.call_tool()`.
- Added a common Parameter entry point. The main `config.json` references Modules/Graphs/Tools/Monitors, and experiment JSON deep-merges overrides. Graph Modules accept only `ctx`, read parameters from `ctx.params`, and call the environment via the process singleton `robotenon.envs.environment`. A separate Monitor registry supports resident/request modes.
- Monitor configuration accepts main keys `monitor_config` and `monitors_config` and instance booleans `mode: "both"` and `resident/request`. Prefer `modes: ["resident", "request"]`.
- Body and EnvironmentAdapter now have separate registries and main-configuration submanifests `body_configs.json` and `envs_configs.json`, using `@register_body` / `@register_environment` respectively. `initialize_body_service` starts Body, `initialize_environment_adapter` connects EnvironmentAdapter, and Sessions remain per Run.

## Compatibility changes

| Previous behavior | New behavior or migration |
| --- | --- |
| backend selected LangGraph or fallback | Both names are accepted, but a single scheduler runs internally without constructing StateGraph |
| import robotenon registered all built-ins | Assembly performs registration. Call load_builtin_components() explicitly before registry inspection if necessary. |
| Imports created an environment-selected database | Assembly or first use of the default facade creates it |
| A run-only Module could receive ModuleContext | run always receives GraphContext. If invoke is also available, call prefers invoke. |
| Mixed edges and predecessors/goto could ignore parts of configuration | The compiler rejects mixed formats. Migrate to one format first. |
| Old checkpoints saved live graph_stack | Live stacks are transient and old in-flight snapshots require explicit handling |
| Identical node_id could overwrite nested Graph outputs | The legacy outputs view remains. scoped_outputs distinguishes full call paths. |
| Unknown checkpoint objects converted to str and malformed models became None | Errors are explicit. Plugins must supply JSON-serializable data. |
| Remote reset/check received the entire GraphContext | ServiceContext omits scheduler fields from cross-service traffic |
| Tool, Memory, Skill registered as Modules | Migrate to `@register_tool` + `tools_configs.json`. Modules access resident instances via `ctx.call_tool()`. |
| Module `run(ctx, env, params)` | Use `run(ctx)`, read parameters through `ctx.params`, and import the `environment` singleton from `robotenon.envs` for environment calls |

Legacy imports including `from robotenon.runtime.safety import SQLiteSafetyStateStore`, `from robotenon.core.context import SQLiteContextStore`, and `from robotenon.services.action_ledger import SQLiteActionExecutionStore` remain available. The implementations live in storage/safety.py, storage/context.py, and storage/actions.py respectively.

## Checkpoint semantics

Checkpoints persist application state and an execution cursor for unfinished nodes. External side effects still need their own completion protocol.

- Resume using an empty GraphContext with the same session_id. initial_values merge after the snapshot loads.
- Skip committed nodes. Uncommitted nodes may execute again. Reuse persisted action_id/idempotency_key for actions.
- Verify Graph and Module configuration fingerprints before resuming. An incompatible snapshot is rejected without silent replay from entry.
- Explicitly invoking run after completion starts a new traversal and preserves application values.
- The caller decides whether to reset or reconcile old in-flight snapshots. Copying a legacy graph_stack is insufficient.
- Reconcile the gap between physical motion and persisted ack using device-side idempotency or a state query.

## Concurrent Modules

Shared ctx identity remains compatible. ExecutionFrame isolates scheduler metadata, but ordinary dict writes gain no implicit transactions.

New plugins should return NodeResult. Explicit patches in one wave require a single writer per key. Legacy plugins writing ctx.values, returning dicts, or emitting external side effects are responsible for conflict avoidance and idempotency. `GraphContext.history` and `scoped_outputs` retain the most recent 2048 entries by default, adjustable at construction. `history_dropped` and `scoped_outputs_dropped` record eviction counts. Trace/Recorder provide durable long-term logging, subject to bounded buffering and possible event loss on failure.

`RunCheckpoint` is a public, versioned DTO (`robotenon.contracts.checkpoint`) separating `checkpoint_version`, Graph fingerprint, CAS revision, cursor, and JSON state. Export with `ctx.to_checkpoint()` and restore with `GraphContext.from_checkpoint(...)`. Task-local `graph_stack` is excluded, and the runtime attempts to save the latest cursor on cancellation.


## Verification

```bash
python -m pytest
Tenon modules-check
Tenon tools-check
Tenon graphs-check
Tenon run-fake --output /tmp/robotenon-smoke
python -m ruff check --select F robotenon
```

Fault regressions live in tests/test_architecture_regressions.py. Local UDS integration requires permission to bind a temporary socket. CI runs on Linux. docs/changes/audit-2026-09-15 preserves the pre-repair audit and hashes. Its reproduce.py uses historical defect assertions.

No hardware was connected and no 100Hz control guarantee was established. Measure SQLite lock contention and SDK tail latency in the deployment environment.

## 2026-09-18 — Unified execution and observability migration

| Setting or feature | Current contract |
| --- | --- |
| Parallel configuration | Use `parameters.runtime.execution`. `backend` is the registered backend name (built-ins `in_process` or `process`). Set concurrency with `max_workers`. |
| Early design parameters | `runtime.parallel.enabled`, `persistent_gpu_workers`, and `max_in_flight` are unimplemented and should be omitted |
| Single tasks and hardware | Share the WorkerRuntime lifecycle. Configuration selects resources and backend. |
| Tool cache | Reuse weights/connections across tasks and clear task references/session state via `end_session(session_id)` |
| Batch memory | Default to snapshot with deterministic merging. For sequentially visible writes select sequential and max_workers=1. |
| Artifact paths | Process runs isolate attempts. Observation and safety resources follow the task root. External services must use those paths. |
| Web data sources | Choose the task or shared database explicitly. Automatic database guessing and JSONL backfill were removed. Configure media indices separately. |
| Automatic Web service | Read-only observation view scoped to run lifetime, with experiment management outside its scope |
| Retries | max_retries applies to execution faults such as process loss. Ordinary task errors are not retried automatically. |
| Recovery | Coordinator crash takeover is unavailable. Explicitly enable Graph ContextStore when needed. |

Experiment/run/episode/attempt identity flows through Trace and Metrics. Older records lacking identity may be unavailable under experiment filters, and the framework does not infer their identities. Metrics still uses JSONL. Shared SQL does not imply shared storage for all metrics and media.

First verify Fake and cleanup across consecutive tasks, then check data sources and configuration, and finally test simulation/hardware with the actual dependencies. The template supplies no vendor driver, and resource locks do not replace hardware protection. See [execution and observability](../execution-and-observability.md) for commands, defaults, and limits.

## 2026-09-18 — Migration after the P1 audit repairs

- Disabling Monitor preserves the configured shared safety state. Existing P3/P4 latches may correctly cause a safety stop. Do not automatically clear the safety database to make a task run.
- Enabling Monitor now runs registered plugins. Previously disconnected checks may begin blocking actions as intended.
- Safety storage adds `scope: "shared" | "task"`. The default shared mode preserves the explicit path. Simulation templates select task, while persistent device safety uses shared.
- Full Portal controls add Bearer token and origin checks. The browser reads the token from the terminal launch link. Existing API clients must send Authorization and JSON Content-Type. Add extra domains through `--allowed-host`.
- Synchronizing files does not update running processes. New launches use the repaired code. Read-only access and control authorization remain distinct.

### Periodic loops and call origin

`goto` allows the first target invocation without waiting for predecessors. `loop` accepts optional `every: "20ms"` in ms/s and repeats immediately if omitted. `ctx.trigger` exposes entry/next/goto/loop so when can avoid reissuing an external chain during self-loops. An unconditional route alongside default emits an unreachable warning. Timers restart on process recovery and missed ticks are not replayed.

### Predecessor completion and Graph lifecycle

next/default implement an AND join: each predecessor must complete and select the target. Duplicate requests during execution retain one follow-up invocation. Recheck loop conditions at expiry against the same read snapshot as new calls. END terminates a branch. The graph.stop system operation stops only the current Graph, including when called inside a subgraph. Checkpoints persist routing generations, predecessor receipts, and requests. In-flight work with old fingerprints must restart explicitly.

### Invocation merging and join diagnostics

A complete next and goto to the same target merge before startup and run with ctx.trigger=next. Partial predecessor arrival does not block goto. GraphResult.routing_diagnostics and episode summaries report missing predecessors and reasons for unreleased joins. Checkpoints preserve the diagnostics. Prefer node/when objects on conditional edges. Legacy syntax remains accepted. A timed loop.when recheck retains the trigger that scheduled it, while the actual self-loop executes with trigger loop.

### Condition and periodic-loop observability

Loading checks static node names in results/result_versions. Correct unknown names. Dynamic application fields are not inferred statically. GraphResult and episode summaries add routing_events, routing_events_dropped, and loop_metrics. Trace/Metrics receive condition events and period statistics, and checkpoints retain the last 200 observations per Graph. All configurations use the current syntax without a separate syntax-version field or multi-version scheme.
