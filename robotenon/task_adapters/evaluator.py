"""Generic ordered experiment execution for Task Adapt providers."""

from __future__ import annotations

import asyncio
import json
import os
import re
import random
import shutil
import tempfile
import time
from collections import Counter
from .result_writer import with_result_writer

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..infra.params import Params
from ..runtime.run_state import reserve_evaluation
from .base import (
    ConfiguredTaskAdapter,
    TaskAdapterRunContext,
    TaskCase,
    TaskCaseRunner,
)


def write_json(path: Path, value: Any) -> None:
    """Write a JSON artifact atomically so status readers never see half JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _ordered_adapter_jobs(adapter_cases):
    """Return one globally shuffled queue when adapters opt into that scope."""
    jobs = [
        (configured, case) for configured, cases in adapter_cases for case in cases
    ]
    schedules = [
        dict(getattr(configured.config, "params", {}).get("scheduling", {}))
        for configured, _ in adapter_cases
    ]
    global_schedules = [s for s in schedules if s.get("scope") == "global"]
    if not global_schedules:
        return jobs
    if len(global_schedules) != len(schedules):
        raise ValueError("global scheduling scope must be enabled for every adapter")
    reference = {
        key: global_schedules[0].get(key, default)
        for key, default in (("strategy", "global_queue"), ("shuffle", True), ("seed", 7))
    }
    if reference["strategy"] != "global_queue":
        raise ValueError("global adapter shuffle requires global_queue scheduling")
    for schedule in global_schedules[1:]:
        current = {
            key: schedule.get(key, default)
            for key, default in (("strategy", "global_queue"), ("shuffle", True), ("seed", 7))
        }
        if current != reference:
            raise ValueError("globally scheduled adapters must use identical settings")
    if reference["shuffle"]:
        random.Random(reference["seed"]).shuffle(jobs)
    plan_paths = {
        configured.config.params.get("episode_plan_path")
        for configured, _ in adapter_cases
        if configured.config.params.get("episode_plan_path")
    }
    if len(plan_paths) > 1:
        raise ValueError("globally scheduled adapters require one episode plan path")
    if plan_paths:
        write_json(
            Path(plan_paths.pop()),
            {
                "scheduling": {**reference, "scope": "global"},
                "expected_episodes": len(jobs),
                "episodes": [
                    {"global_order": index, **case.to_dict()}
                    for index, (_, case) in enumerate(jobs)
                ],
            },
        )
    return jobs


def clear_case_artifacts(path: Path) -> None:
    """Discard the previous execution after its worker has stopped."""
    for artifact in (
        path.with_suffix(""),
        path.with_suffix(".trace"),
        path.with_suffix(".mp4"),
        path.with_suffix(".memory.json"),
        path.parent / ".live" / path.name,
    ):
        if artifact.is_dir() and not artifact.is_symlink():
            shutil.rmtree(artifact)
        else:
            artifact.unlink(missing_ok=True)


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return value or "task"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_known_progress(episode_path: Path, run_id: str) -> dict[str, Any]:
    """Recover a crashed worker's last published progress, never a stale run."""
    path = episode_path.parent / ".live" / episode_path.name
    try:
        live = json.loads(path.read_text(encoding="utf-8"))
        if live.get("identity", {}).get("run_id") != run_id:
            return {}
        context = live.get("context", {})
        steps = context.get("values", {}).get("env_steps")
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 0:
            return {}
        return {
            "env_steps": steps,
            "env_steps_partial": True,
            "last_active_node": context.get("current_node"),
            "last_progress_at": live.get("updated_at"),
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _case_path(
    output_root: Path,
    case: TaskCase,
    *,
    adapter_count: int,
) -> Path:
    task_dir = _safe_name(case.task_id)
    if adapter_count > 1:
        task_dir = str(Path(_safe_name(case.adapter_id)) / task_dir)
    seed = f"_seed_{case.seed}" if case.seed is not None else ""
    return output_root / task_dir / f"episode_{case.episode:03d}{seed}.json"


def _compact_result(result: Any) -> Any:
    """Keep episode metadata out of aggregate files while preserving artifacts."""

    if not isinstance(result, dict):
        return result
    excluded = {
        "context",
        "observations",
        "frames",
        "actions",
        "action_chunk",
        "history",
        "raw",
    }
    return {
        key: value
        for key, value in result.items()
        if key not in excluded and not isinstance(value, (dict, list))
    }


def _episode_graph_runtime(record: Mapping[str, Any]) -> dict[str, Any]:
    result = record.get("result")
    result = result if isinstance(result, Mapping) else {}
    context = result.get("context")
    context = context if isinstance(context, Mapping) else {}
    history = context.get("history")
    history = history if isinstance(history, list) else []
    node_states: dict[str, str] = {}
    node_order: list[str] = []
    graph_id = context.get("current_graph")
    active_node = context.get("current_node")
    for event in history:
        if not isinstance(event, Mapping):
            continue
        event_graph = event.get("graph")
        node_id = event.get("node")
        if event_graph and not graph_id:
            graph_id = event_graph
        if not node_id:
            continue
        node_id = str(node_id)
        if node_id not in node_states:
            node_order.append(node_id)
            node_states[node_id] = "pending"
        if event.get("event") == "enter":
            node_states[node_id] = "running"
            active_node = node_id
        elif event.get("event") == "exit":
            node_states[node_id] = "completed"
            active_node = node_id
        elif event.get("event") == "error":
            node_states[node_id] = "failed"
            active_node = node_id
    status = result.get("graph_status") or record.get("status") or "unknown"
    success = result.get("graph_success")
    if success is None:
        success = status == "completed"
    return {
        "graph_id": str(graph_id) if graph_id else None,
        "status": str(status),
        "success": bool(success),
        "active_node": str(active_node) if active_node else None,
        "nodes": [
            {"id": node_id, "status": node_states[node_id]} for node_id in node_order
        ],
    }


def _compact_episode(record: Mapping[str, Any]) -> dict[str, Any]:
    compact = {
        key: value
        for key, value in record.items()
        if key not in {"result", "context", "observations", "frames", "actions"}
    }
    if "result" in record:
        compact["result"] = _compact_result(record.get("result"))
    compact.setdefault("graph_runtime", _episode_graph_runtime(record))
    return compact


def _compact_stats(stats: Mapping[str, Any]) -> dict[str, Any]:
    compact = {key: value for key, value in stats.items() if key != "episodes"}
    values = stats.get("episodes", [])
    compact["episodes"] = (
        [_compact_episode(item) for item in values if isinstance(item, Mapping)]
        if isinstance(values, list)
        else []
    )
    return compact


def _summary(
    *,
    run_id: str,
    adapters: Sequence[ConfiguredTaskAdapter],
    adapter_stats: Mapping[str, Mapping[str, Any]],
    options: Mapping[str, Any],
) -> dict[str, Any]:
    episodes = [
        episode
        for stats in adapter_stats.values()
        for episode in stats.get("episodes", [])
    ]
    episodes = [
        item for item in episodes if item.get("status") not in {"pending", "running"}
    ]
    completed = [
        episode for episode in episodes if episode.get("status") == "completed"
    ]
    successes = sum(int(item.get("task_success", False)) for item in completed)
    for stats in adapter_stats.values():
        values = [
            item
            for item in stats["episodes"]
            if item.get("status") not in {"pending", "running"}
        ]
        stats["num_completed"] = sum(
            item.get("status") == "completed" for item in values
        )
        stats["num_errors"] = sum(item.get("status") == "error" for item in values)
        stats["successes"] = sum(bool(item.get("task_success")) for item in values)
        stats["success_rate"] = stats["successes"] / len(values) if values else None
    compact_adapter_stats = {
        adapter_id: _compact_stats(stats) for adapter_id, stats in adapter_stats.items()
    }
    return {
        "run_id": run_id,
        "adapters": [item.instance_id for item in adapters],
        "adapter_order": [item.instance_id for item in adapters],
        "num_adapters": len(adapters),
        # ``num_episodes`` is the number of attempted cases, including cases
        # that produced an error record.  Reporting only ``completed`` here
        # made a real failed attempt look like an empty 0/0 experiment.
        "num_episodes": len(episodes),
        "num_completed": len(completed),
        "num_errors": sum(item.get("status") == "error" for item in episodes),
        "num_cancelled": sum(item.get("status") == "cancelled" for item in episodes),
        "successes": successes,
        "episode_success_rate": successes / len(episodes) if episodes else None,
        "options": dict(options),
        "task_adapters": compact_adapter_stats,
    }


@reserve_evaluation
@with_result_writer
async def evaluate_task_adapters(
    *,
    config_path: str | Path,
    params: Params,
    adapters: Sequence[ConfiguredTaskAdapter],
    output_dir: str | Path,
    run_id: str,
    run_episode: TaskCaseRunner,
    memory_path: str | Path | None = None,
    options: Mapping[str, Any] | None = None,
    execution_backend: Any = None,
    on_progress: Callable[[int, int, Mapping[str, Any]], None] | None = None,
    on_case_start: Callable[[int, int, Mapping[str, Any]], None] | None = None,
    on_case_complete: Callable[[int, int, Mapping[str, Any]], None] | None = None,
    on_case_reset: Callable[[str, str], None] | None = None,
    _writer=None,
) -> dict[str, Any]:
    """Execute adapter cases through a backend and persist queryable artifacts.

    Cases are materialised per adapter before execution so the web UI can show
    a deterministic total.  Adapters themselves own task-set expansion; this
    function only enforces the configured adapter order and the common
    episode/result format.
    """

    if not adapters:
        raise ValueError("at least one enabled task adapter is required")
    if len({adapter.instance_id for adapter in adapters}) != len(adapters):
        raise ValueError("duplicate task adapter identities")
    root = Path(output_dir)
    output_root = root / run_id
    output_root.mkdir(parents=True, exist_ok=True)
    from ..runtime.fd_monitor import FileDescriptorMonitor

    fd_monitor = FileDescriptorMonitor(output_root)

    def record_fd_usage(event: str, episode: Mapping[str, Any] | None = None) -> None:
        try:
            fd_monitor.snapshot(event, episode=episode, timestamp=_now())
        except OSError:
            # Monitoring must remain diagnostic under filesystem pressure and
            # must never replace the episode's primary result.
            pass

    resolved_config = Path(config_path).resolve()
    resolved_memory = (
        Path(memory_path)
        if memory_path is not None
        else output_root / "long_term_memory.json"
    )
    effective_options = dict(options or {})
    from ..runtime.execution import SequentialExecutionBackend

    backend = execution_backend or SequentialExecutionBackend()
    memory_mode = effective_options.get("memory_mode", "sequential")
    initial_memory = {"records": []}
    if resolved_memory.exists():
        initial_memory = json.loads(resolved_memory.read_text())
    if memory_mode == "snapshot":
        await _writer.write(output_root / "memory_snapshot.json", initial_memory)
    active = {}

    status_path = output_root / "status.json"

    enumeration_parallelism = min(
        int(effective_options.get("enumeration_parallelism", 1)), len(adapters)
    )
    enumeration_gate = asyncio.Semaphore(enumeration_parallelism)

    async def enumerate_adapter(configured):
        async with enumeration_gate:
            return await asyncio.to_thread(
                lambda: list(
                    configured.adapter.enumerate_cases(
                        adapter_id=configured.instance_id,
                        params=params,
                    )
                )
            )

    # asyncio.gather preserves adapter order even when enumeration completes
    # out of order, keeping global shuffle inputs and artifact paths stable.
    enumerated = await asyncio.gather(
        *(enumerate_adapter(configured) for configured in adapters)
    )

    adapter_cases: list[tuple[ConfiguredTaskAdapter, list[TaskCase]]] = []
    total = 0
    for configured, cases in zip(adapters, enumerated, strict=True):
        if not cases:
            raise ValueError(
                f"task adapter {configured.instance_id!r} produced no task cases"
            )
        if any(
            case.adapter_id != configured.instance_id
            or type(case.index) is not int
            or case.index < 0
            for case in cases
        ):
            raise ValueError(
                "task cases require the configured adapter_id and a nonnegative integer index"
            )
        if len({case.key for case in cases}) != len(cases):
            raise ValueError("task adapters produced duplicate case identities")
        adapter_cases.append((configured, cases))
        total += len(cases)

    adapter_stats: dict[str, dict[str, Any]] = {
        configured.instance_id: {
            "adapter_id": configured.instance_id,
            "provider": configured.config.name,
            "order": index,
            "num_cases": len(cases),
            "episodes": [],
        }
        for index, (configured, cases) in enumerate(adapter_cases)
    }
    paths = [
        _case_path(output_root, case, adapter_count=len(adapter_cases))
        for _, cases in adapter_cases
        for case in cases
    ]
    if len(set(paths)) != len(paths):
        raise ValueError("task adapters produced colliding episode artifact paths")
    control = output_root / ".control"
    control.mkdir(exist_ok=True)
    resume_path = control / "resume.json"
    resume = json.loads(resume_path.read_text()) if resume_path.exists() else None
    if resume is not None:
        resume_path.unlink()
        for configured, cases in adapter_cases:
            for case in cases:
                path = _case_path(output_root, case, adapter_count=len(adapter_cases))
                if path.exists():
                    adapter_stats[configured.instance_id]["episodes"].append(
                        _compact_episode(json.loads(path.read_text()))
                    )
    records = {
        key: {item["global_index"]: item for item in stats["episodes"]}
        for key, stats in adapter_stats.items()
    }
    counts = {key: Counter() for key in records}
    index_ready = False
    completed_count = 0

    def contribution(record):
        status = record.get("status")
        terminal = status not in {"pending", "running", None}
        return {
            "num_episodes": int(terminal),
            "num_completed": int(status == "completed"),
            "num_errors": int(status == "error"),
            "num_cancelled": int(status == "cancelled"),
            "successes": int(
                status == "completed" and bool(record.get("task_success"))
            ),
        }

    for key, values in records.items():
        for record in values.values():
            counts[key].update(contribution(record))
    completed_count = sum(value["num_episodes"] for value in counts.values())

    def remember(record):
        nonlocal completed_count
        key, index = record["adapter_id"], record["global_index"]
        previous = records[key].get(index)
        if previous:
            counts[key].subtract(contribution(previous))
            completed_count -= contribution(previous)["num_episodes"]
        record.setdefault("updated_at", _now())
        records[key][index] = _compact_episode(record)
        counts[key].update(contribution(record))
        completed_count += contribution(record)["num_episodes"]

    def progress_summary():
        aggregate = Counter()
        compact_stats = {}
        for key, stats in adapter_stats.items():
            aggregate.update(counts[key])
            compact_stats[key] = {
                **{k: v for k, v in stats.items() if k != "episodes"},
                **dict(counts[key]),
                "success_rate": counts[key]["successes"] / counts[key]["num_episodes"]
                if counts[key]["num_episodes"]
                else None,
            }
        return {
            "run_id": run_id,
            "adapters": list(records),
            "adapter_order": list(records),
            "num_adapters": len(adapters),
            **dict(aggregate),
            "options": effective_options,
            "episode_success_rate": aggregate["successes"] / completed_count
            if completed_count
            else None,
            "task_adapters": compact_stats,
            "episode_index": "episodes.sqlite3",
        }

    def full_summary():
        for key, stats in adapter_stats.items():
            stats["episodes"] = sorted(
                records[key].values(), key=lambda item: item["global_index"]
            )
        return _summary(
            run_id=run_id,
            adapters=adapters,
            adapter_stats=adapter_stats,
            options=effective_options,
        )

    stopped = set()
    await _writer.write(
        status_path,
        {
            "run_id": run_id,
            "status": "running",
            "config": str(resolved_config),
            "config_hash": params.content_hash,
            "total_episodes": total,
            "completed_episodes": 0,
            "adapter_order": [
                configured.instance_id for configured, _ in adapter_cases
            ],
            "current": None,
            "started_at": _now(),
        },
    )
    record_fd_usage("experiment_start")

    async def execute_case(configured, case):
        nonlocal completed_count
        episode_path = _case_path(
            output_root,
            case,
            adapter_count=len(adapter_cases),
        )
        generation = (
            json.loads(episode_path.read_text()).get("generation_started_at_ns")
            if episode_path.exists()
            else None
        )
        live_status_path = episode_path.parent / ".live" / f"{episode_path.stem}.json"
        # The attempt id is assigned by the execution backend (and may change
        # on retry), but run/episode identity is stable for this case.  Keep
        # that stable identity in the parent status file so the Portal can
        # query spans while the worker is still running.
        live_identity = {
            "experiment_id": resolved_config.stem,
            "run_id": run_id,
            "episode_id": f"{case.adapter_id}:{case.index}:{case.episode}",
            "task_id": case.task_id,
            "adapter_id": case.adapter_id,
        }
        live_case = {
            **case.to_dict(),
            "global_index": case.index,
            "episode_json": str(episode_path),
            "trace_dir": None,
            "live_status_path": str(live_status_path),
            "identity": live_identity,
            "status": "running",
            "generation_started_at_ns": generation,
            "updated_at": _now(),
        }
        trace_dir = (
            episode_path.with_suffix(".trace")
            if bool(effective_options.get("save_traces", False))
            else None
        )
        live_case["trace_dir"] = str(trace_dir) if trace_dir is not None else None
        case_started = time.perf_counter()
        try:
            if case.key not in stopped:
                await _writer.write(
                    episode_path, live_case, record=_compact_episode(live_case)
                )
                remember(live_case)
            await _writer.write(
                status_path,
                {
                    "run_id": run_id,
                    "status": "running",
                    "config": str(resolved_config),
                    "config_hash": params.content_hash,
                    "total_episodes": total,
                    "completed_episodes": completed_count,
                    "adapter_order": [item.instance_id for item, _ in adapter_cases],
                    "summary": progress_summary(),
                    "active": [*active.values(), live_case],
                    "current": live_case,
                },
            )
            if on_case_start is not None:
                on_case_start(
                    completed_count,
                    total,
                    live_case,
                )
            active[case.key] = live_case
            case_memory = resolved_memory
            if memory_mode == "disabled":
                case_memory = None
            elif memory_mode == "snapshot":
                case_memory = episode_path.with_suffix(".memory.json")
                await _writer.write(case_memory, initial_memory)
            context = TaskAdapterRunContext(
                params=params,
                config_path=resolved_config,
                output_root=output_root,
                run_id=run_id,
                memory_path=case_memory,
                run_episode=run_episode,
                options=effective_options,
                case_output=episode_path,
                case_trace_dir=trace_dir,
            )
            case_started = time.perf_counter()
            if case.key in stopped:
                raise asyncio.CancelledError()
            result = dict(await backend.execute(configured, case, context))
            graph_failed = result.get("graph_status") in {"failed", "error"}
            record = {
                "adapter_id": configured.instance_id,
                "task_id": case.task_id,
                "episode": case.episode,
                "global_index": case.index,
                "seed": case.seed,
                "status": "error" if graph_failed else "completed",
                "task_success": not graph_failed
                and bool(result.get("task_success", False)),
                "env_steps": result.get("env_steps", 0),
                "elapsed_s": round(time.perf_counter() - case_started, 3),
                "reason": "graph_error" if graph_failed else result.get("reason"),
                "trace_dir": result.get("trace_dir")
                or (str(trace_dir) if trace_dir is not None else None),
                "episode_json": str(episode_path),
                "result": result,
            }
            if graph_failed:
                record["error"] = result.get("error") or "Graph execution failed"
            record["generation_started_at_ns"] = generation
            record["updated_at"] = _now()
            await _writer.write(episode_path, record, record=_compact_episode(record))
        except asyncio.CancelledError:
            if case.key not in stopped:
                raise
            record = {
                **case.to_dict(),
                "global_index": case.index,
                "status": "cancelled",
                "task_success": False,
                "reason": "user_stop",
                "episode_json": str(episode_path),
                "elapsed_s": round(time.perf_counter() - case_started, 3),
            }
            record.update(_last_known_progress(episode_path, run_id))
            record["generation_started_at_ns"] = generation
            record["updated_at"] = _now()
            await _writer.write(episode_path, record, record=_compact_episode(record))
        except Exception as exc:  # preserve the batch and expose the case error
            record = {
                "adapter_id": configured.instance_id,
                "task_id": case.task_id,
                "episode": case.episode,
                "global_index": case.index,
                "seed": case.seed,
                "status": "error",
                "task_success": False,
                "env_steps": 0,
                "elapsed_s": round(time.perf_counter() - case_started, 3),
                "error": f"{type(exc).__name__}: {exc}",
                "trace_dir": str(trace_dir) if trace_dir is not None else None,
                "episode_json": str(episode_path),
            }
            record.update(_last_known_progress(episode_path, run_id))
            record["generation_started_at_ns"] = generation
            record["updated_at"] = _now()
            await _writer.write(episode_path, record, record=_compact_episode(record))

        active.pop(case.key, None)
        remember(record)
        record_fd_usage(
            "episode_complete",
            {
                "adapter_id": case.adapter_id,
                "task_id": case.task_id,
                "global_index": case.index,
                "status": record.get("status"),
            },
        )
        current_summary = progress_summary()
        await _writer.write(
            status_path,
            {
                "run_id": run_id,
                "status": "running",
                "config": str(resolved_config),
                "config_hash": params.content_hash,
                "total_episodes": total,
                "completed_episodes": completed_count,
                "adapter_order": [item.instance_id for item, _ in adapter_cases],
                "last": _compact_episode(record),
                "summary": current_summary,
                "current": next(iter(active.values()), None),
                "active": list(active.values()),
            },
        )
        if on_case_complete is not None:
            on_case_complete(completed_count, total, record)
        if on_progress is not None:
            on_progress(completed_count, total, current_summary)

    from ..runtime.task_control import control_lock

    all_jobs = _ordered_adapter_jobs(adapter_cases)

    def matches(case, targets):
        return targets is None or any(
            target["adapter_id"] == case.adapter_id
            and target["global_index"] == case.index
            for target in targets
        )

    pending = [
        (configured, case)
        for configured, case in all_jobs
        if resume is None or matches(case, resume.get("tasks"))
    ]
    running = {}

    async def reset_case(configured, case):
        path = _case_path(output_root, case, adapter_count=len(adapter_cases))
        if path.exists() and on_case_reset is not None:
            on_case_reset(run_id, f"{case.adapter_id}:{case.index}:{case.episode}")
        await _writer.run(clear_case_artifacts, path)
        record = {
            **case.to_dict(),
            "global_index": case.index,
            "status": "pending",
            "task_success": False,
            "episode_json": str(path),
            "generation_started_at_ns": time.time_ns(),
            "updated_at": _now(),
        }
        await _writer.write(
            path, record, record=_compact_episode(record) if index_ready else None
        )
        live = path.parent / ".live" / path.name
        live.unlink(missing_ok=True)
        remember(record)

    for configured, case in pending:
        await reset_case(configured, case)
    await _writer.initialize(
        item for values in records.values() for item in values.values()
    )
    index_ready = True

    async def publish_summary():
        # Constant-size counts are sufficient during execution. Full history
        # is written once at termination and remains available in the index.
        await _writer.write(output_root / "summary.json", progress_summary())

    await publish_summary()
    ready = control / "ready.json"
    with control_lock(control):
        await _writer.write(ready, {"pid": os.getpid()})
    try:
        while True:
            with control_lock(control):
                commands = []
                for command_path in sorted(control.glob("command-*.json")):
                    commands.append(json.loads(command_path.read_text()))
                    command_path.unlink()
                if not pending and not running and not commands:
                    ready.unlink(missing_ok=True)
                    break
            for command in commands:
                selected = [
                    (configured, case)
                    for configured, case in all_jobs
                    if matches(case, command.get("tasks"))
                ]
                if command["action"] == "stop":
                    eligible = set(running) | {case.key for _, case in pending}
                    selected = [
                        (configured, case)
                        for configured, case in selected
                        if case.key in eligible
                    ]
                keys = {case.key for _, case in selected}
                pending = [
                    (configured, case)
                    for configured, case in pending
                    if case.key not in keys
                ]
                stopped.update(keys)
                running_keys = set(running)
                cancelled = [running[key] for key in keys if key in running]
                for task in cancelled:
                    task.cancel()
                cancellation_results = await asyncio.gather(
                    *cancelled, return_exceptions=True
                )
                for error in cancellation_results:
                    if isinstance(error, BaseException) and not isinstance(
                        error, asyncio.CancelledError
                    ):
                        raise error
                for key in keys:
                    running.pop(key, None)
                if command["action"] == "rerun":
                    stopped.difference_update(keys)
                    for configured, case in selected:
                        await reset_case(configured, case)
                    pending.extend(selected)
                else:
                    for configured, case in selected:
                        if case.key not in running_keys:
                            await execute_case(configured, case)
                await publish_summary()
            for key, task in list(running.items()):
                if task.done():
                    task.result()
                    del running[key]
            from ..runtime.scheduling import refill_slots
            refill_slots(pending, running, backend.max_workers,
                         lambda job: execute_case(*job), lambda job: job[1].key)
            if pending or running:
                await asyncio.wait(
                    running.values(), timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                )
    except BaseException:
        for task in running.values():
            task.cancel()
        await asyncio.gather(*running.values(), return_exceptions=True)
        await _writer.write(
            output_root / "summary.json", await _writer.run(full_summary)
        )
        await _writer.write(
            status_path,
            {
                "run_id": run_id,
                "status": "cancelled",
                "total_episodes": total,
                "completed_episodes": completed_count,
                "active": [],
                "current": None,
            },
        )
        raise
    finally:
        with control_lock(control):
            ready.unlink(missing_ok=True)
    final_summary = await _writer.run(full_summary)
    if memory_mode == "snapshot":
        memory_records = list(initial_memory.get("records", []))
        for configured, _ in adapter_cases:
            for record in adapter_stats[configured.instance_id]["episodes"]:
                # Load only one full result at a time; compact records deliberately
                # omit nested model output and observations.
                full_record = await _writer.run(
                    lambda path=record["episode_json"]: json.loads(
                        Path(path).read_text()
                    )
                )
                lesson = full_record.get("result", {}).get("ltm_summary")
                if lesson:
                    memory_records.append(lesson)
        await _writer.write(resolved_memory, {"records": memory_records})

    if completed_count != total:
        raise RuntimeError(
            f"experiment cannot complete: {completed_count}/{total} cases are terminal"
        )
    await _writer.write(output_root / "summary.json", final_summary)
    # Preserve per-task stats without copying the entire adapter into every task directory.
    grouped = {}
    for values in records.values():
        for record in values.values():
            grouped.setdefault(Path(record["episode_json"]).parent, []).append(record)
    for directory, values in grouped.items():
        totals = Counter()
        for item in values:
            totals.update(contribution(item))
        stats = adapter_stats[values[0]["adapter_id"]]
        await _writer.write(
            directory / "stats.json",
            _compact_stats(
                {
                    **stats,
                    **dict(totals),
                    "num_cases": len(values),
                    "episodes": values,
                    "success_rate": totals["successes"] / totals["num_episodes"]
                    if totals["num_episodes"]
                    else None,
                }
            ),
        )
    await _writer.write(
        status_path,
        {
            "run_id": run_id,
            "status": "completed",
            "config": str(resolved_config),
            "config_hash": params.content_hash,
            "total_episodes": total,
            "completed_episodes": completed_count,
            "adapter_order": [item.instance_id for item, _ in adapter_cases],
            "summary": progress_summary(),
            "current": None,
            "ended_at": _now(),
        },
    )
    return final_summary


__all__ = ["evaluate_task_adapters", "write_json"]
