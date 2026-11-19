# Verification record for September 2026

[**English**](verification-2026-09.md) · [简体中文](verification-2026-09.zh-CN.md)

## 2026-09-20 — Integration and infrastructure decoupling

- Full local macOS regression with Python 3.11.10: **440 passed, 1 skipped, 1 warning in 21.00 seconds**.
  The existing Graph validation case warns about an unreachable default route.
- External plugins: serial and spawned multiprocess Environment/TaskAdapter cases, single/batch custom execution backends, assembly/close for all four stores, failed startup cleanup, action-store restart, Environment RPC and independent Body control on custom stream transport, and cached round trips for custom observation modalities.
- Architecture tests require capability-based assembly and prohibit importing the process implementation or branching by concrete backend name at entry points. The observation pipeline depends on the store protocol.
- Local links across 27 Markdown files resolved, 29 JSON blocks parsed, and XML report fragments parsed.
- New infrastructure code, key changed files, and architecture tests passed Ruff F rules. `git diff --check` passed.

Tests used simulated plugins, temporary stores, and local sockets. No LIBERO simulation, GPU model, physical robot, cross-host deployment, or restart of existing remote services occurred. See [infrastructure plugins](../infrastructure-plugins.md) and the [integration review](plugin-extension-audit.md).

## 2026-09-18 — P1 fixes, per-Worker model deployment, and documentation

- Following P1 repairs, full suites on local and server both reported **196 passed**. Coverage included configured safety store with Monitor disabled, registered Monitor assembly, Portal control authorization, and preserving external same-named heartbeats when Monitor had not started.
- With per-Worker model deployment, local Python 3.11 reported **209 passed**. A previous server full run on Python 3.12.11 reported **207 passed in 67.37 seconds**. New Monitor/Body environment demos passed locally and separately on the server. Cases covered GPU-local mapping, port isolation, service reuse, startup failure/cancellation, abnormal Worker cleanup, client endpoint binding, configuration validation, and EGL parameter forwarding.
- Three complete Python examples executed: Module output and patch, Monitor → safety state → Body interrupt, and TaskQueue claim/retry/complete. The interrupt example verified that a later P0 report did not clear a P3 latch. No physical robot was connected.
- Local links across 17 Markdown files, syntax of 86 JSON/command blocks, and XML fragments passed. Ruff F rules and git diff --check passed.

See [Module reference](../module-guide.md), [Evaluator/Monitor/TaskQueue](../evaluation-monitoring-and-task-queue.md), and [Worker model deployment](../worker-model-deployment.md). Deployment regressions used lightweight substitute services. No real six-GPU weight loading, memory/throughput stress, or hardware actions were tested. Synchronized code becomes active only on a later service start or restart.

## 2026-09-18 — Execution and observability migration

| Environment | Result |
| --- | --- |
| Local Python 3.11 | Full suite: 179 passed |
| Server Python 3.12.11, model-runtime environment | Full suite: 179 passed in approximately 33 seconds |
| Sync destination | Server `/path/to/deployment`, preserving run data and existing services |

Coverage included resident Workers, isolation across tasks, process loss/timeout/cancellation, resource reservation, deterministic memory merges, shared observation sources, read-only queries, and existing architecture regressions. The Chinese documentation update checked local links in 13 Markdown files, syntax of 75 JSON/command blocks, CLI parameters, and XML fragment formatting. Fake smoke returned completed / success=true / 16 steps. Documentation checks were separate from the reported full-suite run.

No real GPU/EGL parallel load, model memory test, hardware motion, or emergency stop was tested. Existing model and Web services were not restarted. Coordinator crash takeover and zero-loss telemetry were outside the implementation.

To verify in the current Python environment:

```bash
python -m pytest -q
```

See [execution and observability](../execution-and-observability.md) and the [migration notes](migration-2026-09.md).

## 2026-09-16 — Historical verification

The following checks report results from that day and were not all rerun for subsequent updates.

Local Python 3.11.10. Verification date: 2026-09-16.

| Check | Result |
| --- | --- |
| Full pytest | 130 passed, 3 skipped (macOS sandbox disallowed UDS sockets) |
| Python 3.10 Torch environment imports/compileall | Passed, including Tool registration, configuration, model calls, and Pydantic 1 compatible imports |
| Local UDS | Tests retained. Socket-binding cases skipped under the sandbox. |
| Ruff F rules for source and new architecture tests | Passed |
| Targeted Mypy for execution/scheduler/compiler (follow-imports=skip) | Passed. Limited to the specified modules. |
| modules-check / tools-check / monitors-check / graphs-check | Passed |
| run_fake | completed / success=True / 3 steps |
| examples.service_runtime | completed / success=True |
| Wheel build | Succeeded without downloading dependencies |
| Isolated import from wheel | Succeeded, including static HTML/CSS/JS and legacy storage import aliases |

New tests covered latches, loop fork/join, parallel subgraph identity/location, checkpoint resume/corruption rejection, failed-branch cancellation, RPC control and total deadline, cancelled Module startup cleanup, P2 admission, import side effects, NodeResult write conflicts, ServiceContext boundaries, dual-mode Monitor registration/configuration/instance reuse/safety publishing, and resident ToolRuntime registration, configuration, lifecycle, model/memory/skill calls, and GraphContext injection.

The historical CI configuration lived in the outer `system/.github/workflows/robotenon.yml`, outside this standalone RoboTenon repository. It defined Python 3.10–3.12, Pydantic 1/2, and with/without LangGraph combinations. This pass did not trigger remote CI or run Python 3.12 locally. Imports/compileall and Pydantic 1 compatibility smoke ran in the Python 3.10 Torch environment.

No robots were connected, services released/deployed, or hardware failure/load tests run. See migration-2026-09.md for semantics and upgrade notes.
