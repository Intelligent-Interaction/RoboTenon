"""Configuration-driven single-episode execution.

This module is the composition boundary for an experiment.  It does not know
about RoboCasa, Xiaomi, or any other vendor implementation: the selected
Environment, Body, Modules, Tools, Monitors, and Graphs are all resolved from
the master config and their registries.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..envs import bind_environment, environment
from ..contracts.serialization import jsonable
from ..infra.params import Params, merge_parameter_values
from ..runtime.config import LoadedConfigs, load_request_config
from ..runtime.factory import build_environment_adapter, initialize_application
from ..runtime.terminal import TerminalLogger
from ..schemas import SessionSpec
from ..services import (
    ObservationEnvironmentService,
    ServiceSupervisor,
)
from ..workflow import GraphContext, GraphRuntime
from ..workflow.bindings import graph_outputs
from .observability import ExecutionIdentity, ObservabilityRuntime, ObservabilitySource


@dataclass(slots=True)
class EpisodeRequest:
    """Explicit overrides for one config-driven episode."""

    config: str | Path = "configs/config.json"
    experiment_config: str | Path | None = None
    task_id: str | None = None
    env_id: str | None = None
    split: str | None = None
    seed: int | None = None
    horizon: int | None = None
    model_path: str | None = None
    model_host: str | None = None
    model_port: int | None = None
    dry_run: bool | None = None
    output: str | Path | None = None
    video_path: str | Path | None = None
    video_stride: int | None = None
    video_fps: int | None = None
    camera_width: int | None = None
    camera_height: int | None = None
    trace_dir: str | Path | None = None
    metrics_dir: str | Path | None = None
    memory: str | Path | None = None
    graph_id: str | None = None
    identity: ExecutionIdentity | None = None
    observability: ObservabilitySource | None = None
    # Optional per-episode file used by the Portal to render a running case.
    # It is deliberately separate from the final episode artifact so a live
    # writer never races the evaluator's completed-result write.
    live_status_path: str | Path | None = None
    memory_enabled: bool = True
    resource_root: str | Path | None = None
    # Provider-owned session options; never interpreted by the runner.
    environment_parameters: dict[str, Any] | None = None
    config_snapshot: dict[str, Any] | str | Path | None = None
    shared_model_endpoints: dict[str, str] | None = None


def _required_param(params: Any, name: str, expected: type) -> Any:
    value = params.get(name, [expected, None])
    if value is None:
        raise ValueError(f"missing required parameter: {name}")
    return value


def _apply_request_overrides(
    params: Params,
    request: EpisodeRequest,
    *,
    case: Any = None,
) -> Params:
    """Merge an optional TaskCase and explicit request values into ctx.params."""

    sections: dict[str, dict[str, Any]] = {}

    def put(section: str, key: str, value: Any) -> None:
        if value is not None:
            sections.setdefault(section, {})[key] = value

    if case is not None:
        for key, value in dict(getattr(case, "parameters", {}) or {}).items():
            put("task", str(key), value)
        put("task", "task_id", getattr(case, "task_id", None))
        put("task", "env_id", getattr(case, "environment_id", None))
        put("task", "seed", getattr(case, "seed", None))
        put("runtime", "horizon", getattr(case, "horizon", None))

    # Keep the TaskCase wire format stable: provider options travel in its
    # existing parameters object, separate from unrelated task metadata.
    case_environment = sections.get("task", {}).get("environment_parameters")
    for source, options in (
        ("TaskCase.parameters.environment_parameters", case_environment),
        ("EpisodeRequest.environment_parameters", request.environment_parameters),
    ):
        if options is not None and not isinstance(options, Mapping):
            raise ValueError(f"{source} must be a JSON object")
    environment_parameters = merge_parameter_values(
        params.get("environment.parameters", [dict, None]),
        case_environment,
    )
    environment_parameters = merge_parameter_values(
        environment_parameters, request.environment_parameters,
    )
    if environment_parameters:
        put("environment", "parameters", environment_parameters)

    put("task", "task_id", request.task_id)
    put("task", "env_id", request.env_id)
    put("task", "split", request.split)
    put("task", "seed", request.seed)
    put("runtime", "horizon", request.horizon)
    put("runtime", "dry_run", request.dry_run)
    put("runtime", "output_path", str(request.output) if request.output is not None else None)
    put("runtime", "trace_dir", str(request.trace_dir) if request.trace_dir is not None else None)
    put("runtime", "metrics_dir", str(request.metrics_dir) if request.metrics_dir is not None else None)
    put("runtime", "memory_path", str(request.memory) if request.memory is not None else None)
    put("runtime", "graph_id", request.graph_id)
    put("model", "path", request.model_path)
    put("model", "host", request.model_host)
    put("model", "port", request.model_port)
    put("render", "video_path", str(request.video_path) if request.video_path is not None else None)
    put("render", "video_stride", request.video_stride)
    put("render", "video_fps", request.video_fps)
    put("render", "camera_width", request.camera_width)
    put("render", "camera_height", request.camera_height)
    return Params(
        merge_parameter_values(params.values, sections),
        name=params.name,
        parent_hash=params.content_hash,
    )


def load_long_term_memory(
    path: Path | None,
    *,
    limit: int | None = 3,
) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    records = value.get("records", []) if isinstance(value, dict) else value
    values = [dict(item) for item in records if isinstance(item, dict)]
    return values if limit is None else values[-limit:]


async def _summarize_episode(
    context: GraphContext,
    tools: Any,
    memory_path: Path,
    outputs: dict[str, Any],
) -> dict[str, Any]:
    """Persist an optional reusable lesson without coupling the runner to a model."""

    params = context.params
    if params is None:
        raise RuntimeError("episode summary requires the shared Params object in ctx.params")
    task_id = _required_param(params, "task.task_id", str)
    hlp_tool = params.get("model.hlp_tool", [str, None])
    if not hlp_tool:
        return {}
    task = str(
        (context.feedback.state if context.feedback is not None else {}).get(
            "annotation.human.task_description"
        )
        or task_id
    )
    stm = outputs.get("stm", {})
    vlm_success = bool(
        outputs.get("subtask_success")
        and not (stm.get("remaining", []) if isinstance(stm, dict) else [])
    )
    schema = {
        "outcome": "success|failure|uncertain",
        "reusable_lesson": "one concise lesson",
        "applicable_when": ["conditions"],
        "confidence": 0.0,
    }
    prompt = (
        "Summarize this completed robot episode into one reusable experience. "
        "Do not use or infer environment reward. Generalize beyond object names "
        "where possible. Return JSON only.\n"
        f"Task: {task}\n"
        f"STM: {json.dumps(stm, ensure_ascii=False, default=str)}\n"
        f"Final VLM success: {vlm_success}\n"
        f"Schema: {json.dumps(schema, ensure_ascii=False)}"
    )
    result = await tools.call(
        hlp_tool,
        {
            "task": task,
            "prompt": prompt,
            "system_prompt": "You summarize embodied robot experience for later reuse. Return JSON only.",
            "image_keys": [
                "video.robot0_agentview_left",
                "video.robot0_eye_in_hand",
            ],
        },
        context=context,
    )
    output = result.output if result.ok and isinstance(result.output, dict) else {}
    try:
        confidence = float(output.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    summary = {
        "task": task,
        "outcome": str(output.get("outcome", "uncertain")),
        "reusable_lesson": str(output.get("reusable_lesson", "")),
        "applicable_when": [str(item) for item in output.get("applicable_when", [])]
        if isinstance(output.get("applicable_when", []), (list, tuple))
        else [],
        "confidence": confidence,
    }
    records = load_long_term_memory(memory_path, limit=None)
    records.append(summary)
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = memory_path.with_suffix(memory_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"records": records}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(memory_path)
    return summary


def _environment_selector(loaded: LoadedConfigs, params: Params) -> str:
    for key in (
        "runtime.environment_id",
        "runtime.environment",
        "environment.adapter_id",
        "environment.id",
    ):
        value = params.get(key, [str, None])
        if value:
            return str(value)
    enabled = [item for item in loaded.environments.environments if item.enabled]
    if len(enabled) == 1:
        return str(enabled[0].id or enabled[0].name)
    names = ", ".join(str(item.id or item.name) for item in enabled)
    raise ValueError(
        "the config must select runtime.environment_id when multiple environments "
        f"are enabled ({names or '<none>'})"
    )


def _graph_selector(loaded: LoadedConfigs, params: Params, request: EpisodeRequest) -> str:
    selected = request.graph_id or params.get("runtime.graph_id", [str, None])
    graphs = loaded.graphs.graph_map()
    if selected:
        if selected not in graphs:
            raise KeyError(
                f"unknown configured graph {selected!r}; available: {', '.join(sorted(graphs))}"
            )
        return str(selected)
    if len(graphs) == 1:
        return next(iter(graphs))
    raise ValueError(
        "the config must set runtime.graph_id when multiple graphs are enabled"
    )


def _session_parameters(
    params: Params,
    *,
    env_id: str,
    split: str | None,
) -> dict[str, Any]:
    """Pass provider options through, preserving canonical option precedence.

    Config, case and request options are merged by _apply_request_overrides.
    Existing task/IO/render fields win collisions for backwards compatibility.
    """

    values = params.get("environment.parameters", [dict, None]) or {}
    values["task_env_id"] = env_id
    environment_io = params.get("environment.io", [dict, None])
    if environment_io is not None:
        values["environment_io"] = environment_io
    if split is not None:
        values["split"] = split
    for name in (
        "video_path",
        "video_stride",
        "video_fps",
        "camera_width",
        "camera_height",
    ):
        value = params.get(f"render.{name}", [object, None])
        if value is not None:
            values[name] = value
    return values


async def execute_episode(
    request: EpisodeRequest,
    *,
    case: Any = None,
    loaded: LoadedConfigs | None = None,
    terminal: TerminalLogger | None = None,
    tools: Any = None,
) -> dict[str, Any]:
    """Run one case using only the components selected by the master config."""

    episode_started = time.perf_counter()
    config_path = Path(request.config)
    loaded = loaded or load_request_config(request)
    params = _apply_request_overrides(loaded.params, request, case=case)
    graph_id = _graph_selector(loaded, params, request)
    from .result_projection import require_episode_outputs
    require_episode_outputs(loaded.graphs.graph_map()[graph_id])
    task_id = _required_param(params, "task.task_id", str)
    env_id = _required_param(params, "task.env_id", str)
    split = params.get("task.split", [str, None])
    seed = params.get("task.seed", [int, None])
    horizon = params.get("runtime.horizon", [int, None])
    monitor_enabled = bool(params.get("runtime.monitor_enabled", [bool, False]))
    dry_run = bool(params.get("runtime.dry_run", [bool, False]))
    output_path_value = params.get("runtime.output_path", [str, None])
    output_path = Path(str(output_path_value)) if output_path_value else Path("data/runs/result.json")
    video_stride = int(params.get("render.video_stride", [int, 1]))
    video_fps = int(params.get("render.video_fps", [int, 20]))
    camera_width = int(params.get("render.camera_width", [int, 256]))
    camera_height = int(params.get("render.camera_height", [int, 256]))
    trace_dir_value = params.get("runtime.trace_dir", [str, None])
    metrics_dir_value = params.get("runtime.metrics_dir", [str, None])
    memory_path_value = params.get("runtime.memory_path", [str, None])
    monitor_poll_hz = int(params.get("service.monitor_poll_hz", [int, 5]))
    supervisor_poll_hz = int(params.get("service.supervisor_poll_hz", [int, 10]))
    if horizon is not None and int(horizon) < 1:
        raise ValueError("runtime.horizon must be at least 1")
    if video_stride < 1 or video_fps < 1:
        raise ValueError("render.video_stride and render.video_fps must be positive")
    if camera_width < 1 or camera_height < 1:
        raise ValueError("render.camera_width and render.camera_height must be positive")
    if monitor_poll_hz <= 0 or supervisor_poll_hz <= 0:
        raise ValueError("service polling frequencies must be positive")

    adapter = build_environment_adapter(
        loaded.environments,
        environment_id=_environment_selector(loaded, params),
    )
    identity = request.identity or ExecutionIdentity.create(
        task_id=task_id, experiment_id=config_path.stem,
        adapter_id=getattr(case, "adapter_id", "single"),
    )
    trace_dir = Path(str(trace_dir_value)) if trace_dir_value else None
    metrics_dir = Path(str(metrics_dir_value)) if metrics_dir_value else None
    source = request.observability or ObservabilitySource.from_params(
        params, trace_dir=trace_dir, metrics_dir=metrics_dir,
    )
    from ..task_adapters.evaluator import write_json
    if source.trace_db or source.trace_wal:
        write_json(output_path.with_suffix(".observability.json"), source.to_dict())
    live_status_path = (
        Path(request.live_status_path).expanduser().resolve()
        if request.live_status_path is not None
        else None
    )
    live_history: list[dict[str, Any]] = []
    live_spans: dict[str, dict[str, Any]] = {}
    live_terminal_written = False
    live_span_sequence = 0
    live_last_write: float | None = None
    live_span_limit = 256
    live_write_interval_s = 1.0

    def _live_json(value: Any) -> Any:
        try:
            return jsonable(value)
        except (TypeError, ValueError):
            return str(value)

    def _live_key(event: Mapping[str, Any]) -> str:
        path = event.get("path")
        if isinstance(path, (list, tuple)):
            path_text = "/".join(str(item) for item in path)
        else:
            path_text = str(path or event.get("step") or len(live_history))
        return f"{event.get('graph', '')}:{event.get('node', '')}:{path_text}"

    def _write_live_status(
        ctx: GraphContext,
        *,
        event: Mapping[str, Any] | None = None,
        session_id: Any = None,
        status: str = "running",
        error: str | None = None,
    ) -> None:
        """Publish a compact, atomic snapshot consumed by the Portal.

        Trace spans are intentionally emitted when they close.  This file is
        the complementary live channel: it records the active node and the
        latest context/observation identity while that span is still open.
        It is per episode and therefore cannot affect the single-task path
        when no ``live_status_path`` was requested.
        """

        nonlocal live_terminal_written, live_span_sequence, live_last_write
        if live_status_path is None:
            return
        timestamp = datetime.now(timezone.utc).isoformat()
        if event is not None:
            live_event = dict(event)
            live_event.setdefault("timestamp", timestamp)
            live_history.append(live_event)
            del live_history[:-256]
            key = _live_key(live_event)
            entry = live_spans.get(key)
            event_name = str(live_event.get("event") or "").lower()
            if event_name in {"enter", "start", "started", "running"} or entry is None:
                if entry is None:
                    live_span_sequence += 1
                    entry = {
                        "span_id": f"live:{live_span_sequence}:{key}",
                        "trace_id": str(identity.run_id),
                        "parent_span_id": None,
                        "name": f"node:{live_event.get('graph', '')}:{live_event.get('node', '')}",
                        "start_time": str(live_event.get("timestamp") or timestamp),
                        "end_time": str(live_event.get("timestamp") or timestamp),
                        "status": "running",
                        "attributes": {
                            "kind": "node",
                            "graph": live_event.get("graph"),
                            "node": live_event.get("node"),
                            "step_index": live_event.get("step"),
                            "step": live_event.get("step"),
                            "path": live_event.get("path"),
                        },
                    }
                    live_spans[key] = entry
                    while len(live_spans) > live_span_limit:
                        del live_spans[next(iter(live_spans))]
            if entry is not None:
                entry["end_time"] = str(live_event.get("timestamp") or timestamp)
                if event_name in {"exit", "completed", "complete", "finish", "finished", "success"}:
                    entry["status"] = "ok"
                elif event_name in {"error", "failed", "failure"}:
                    entry["status"] = "error"
                    if live_event.get("error"):
                        entry["attributes"]["error"] = str(live_event["error"])

        # Keep event collection cheap and bounded, even between snapshots.
        # Terminal/error snapshots bypass throttling so final results remain
        # visible immediately. This does not throttle the Graph or observations.
        now = time.monotonic()
        event_failed = event is not None and str(event.get("event", "")).lower() in {
            "error", "failed", "failure"
        }
        if (status == "running" and not error and not event_failed
                and live_last_write is not None
                and now - live_last_write < live_write_interval_s):
            return

        effective_session = session_id or getattr(ctx, "session_id", None)
        if effective_session is None and env_service is not None:
            effective_session = getattr(env_service, "session_id", None)
        context_snapshot = {
            "session_id": _live_json(effective_session),
            "task_id": ctx.task_id,
            "history": [_live_json(item) for item in live_history[-256:]],
            "current_node": getattr(ctx, "current_node", None),
            "current_graph": getattr(ctx, "current_graph", None),
            "step": int(getattr(ctx, "step", 0) or 0),
            "status": status,
            "error": error,
            "feedback": _live_json(getattr(ctx, "feedback", None)),
            "values": _live_json(graph_outputs(loaded.graphs.graph_map()[graph_id], ctx, partial=True)),
        }
        payload = {
            "status": status,
            "updated_at": timestamp,
            "identity": identity.to_dict(),
            "session_id": _live_json(effective_session),
            "feedback": context_snapshot["feedback"],
            "context": context_snapshot,
            "graph_runtime": {
                "graph_id": context_snapshot["current_graph"],
                "status": status,
                "success": False if status == "running" else status == "completed",
                "active_node": context_snapshot["current_node"],
            },
            "timeline_spans": [_live_json(item) for item in live_spans.values()],
            "timeline_window": {"limit": live_span_limit, "total_spans": live_span_sequence,
                                "truncated": live_span_sequence > len(live_spans)},
        }
        if error:
            payload["error"] = str(error)
        write_json(live_status_path, payload)
        live_last_write = time.monotonic()
        if status not in {"running", "starting"}:
            live_terminal_written = True

    def _graph_event(ctx: GraphContext, event: Mapping[str, Any], result: Any = None) -> None:
        if terminal is not None:
            terminal.graph_event(ctx, event, result)
        _write_live_status(ctx, event=event)

    observability = ObservabilityRuntime(source, identity)
    session = None
    trace_client = metrics_client = None

    history_limit = params.resolve("runtime.context_history_limit")
    outputs_limit = params.resolve("runtime.context_outputs_limit")
    history_limit = 2048 if history_limit is None else int(history_limit)
    outputs_limit = 2048 if outputs_limit is None else int(outputs_limit)
    if history_limit < 1 or outputs_limit < 1:
        raise ValueError("runtime context history/output limits must be positive")
    context = GraphContext(
        task_id=task_id,
        params=params,
        history_limit=history_limit,
        scoped_outputs_limit=outputs_limit,
    )
    app = None
    env_service = None
    monitor = None
    supervisor = None
    previous_environment = None
    environment_bound = False
    try:
        await observability.start()
        trace_client, metrics_client = observability.trace, observability.metrics
        trace_mode = params.resolve("runtime.trace_mode") or "full"
        if trace_mode not in {"full", "summary"}:
            raise ValueError("runtime.trace_mode must be full or summary")
        if trace_client is not None:
            trace_client.mode = trace_mode
        _write_live_status(context, status="starting")
        session = await adapter.open(
            SessionSpec(task_id=task_id,
                        environment_id=str(getattr(adapter.descriptor, "env_id", "environment")),
                        seed=seed, parameters=_session_parameters(params, env_id=env_id, split=split))
        )
        _write_live_status(context, session_id=getattr(session, "session_id", None))
        session_stop = getattr(session, "stop", None)
        body_runtime: dict[str, Any] = {
            "session": session,
            "observation_updates": context.observation_updates,
        }
        # Action execution belongs to the selected Body provider.  The runner
        # only passes the session for environment observations and an optional
        # lifecycle stop hook for the safety supervisor.  RoboCasa365Body
        # creates its simulator driver from this session; real bodies receive
        # their own SDK/ROS2 driver instead.
        if callable(session_stop):
            body_runtime["stop_executor"] = session_stop
        app = await initialize_application(
            config_path,
            loaded=loaded,
            experiment_config=request.experiment_config,
            environment_service=session,
            body_runtime=body_runtime,
            start_monitors=False,
            metrics=metrics_client,
            tools=tools,
            resource_root=request.resource_root,
        )
        env_service = (ObservationEnvironmentService(session, app.observation_pipeline)
                       if app.observation_pipeline is not None else session)
        if monitor_enabled:
            monitor = app.monitors
            services = {"env": env_service, "monitor": monitor}
            if app.body is not None:
                services["body"] = app.body
            supervisor = ServiceSupervisor(
                services,
                safety_state=app.safety_state,
                poll_hz=supervisor_poll_hz,
            )
        previous_environment = bind_environment(env_service)
        environment_bound = True
        runtime = GraphRuntime(
            loaded.graphs.graph_map(),
            app.modules,
            tools=app.tools,
            params=params,
            safety_state=app.safety_state,
            backend="fallback",
            context_store=app.context_store,
            metrics=metrics_client,
            trace=trace_client,
            event_callback=_graph_event if (terminal is not None or live_status_path is not None) else None,
        )
        memory_path = Path(str(memory_path_value)) if memory_path_value and request.memory_enabled else None
        context.values.update(
            {
                "env_steps": 0,
                "ltm_records": load_long_term_memory(memory_path),
            }
        )
        if supervisor is not None:
            await supervisor.start()
        result = await runtime.run(
            graph_id,
            ctx=context,
            body=app.body,
            monitor=monitor,
        )
        _write_live_status(
            result.context,
            session_id=getattr(result.context, "session_id", None),
            status=str(result.status),
            error=result.error,
        )
        live_terminal_written = True
        final_feedback = result.context.feedback
        ltm_summary = None
        if memory_path is not None and result.success and final_feedback is not None:
            try:
                ltm_summary = await _summarize_episode(result.context, app.tools, memory_path, result.outputs)
            except Exception as exc:
                # Optional learning must not replace an episode's primary result.
                ltm_summary = {"error": f"{type(exc).__name__}: {exc}"}
        session_video = getattr(session, "video_path", None)
        summary = {
            "graph_id": graph_id,
            "trace_mode": trace_mode,
            "graph_status": result.status,
            "graph_success": result.success,
            "routing_diagnostics": result.routing_diagnostics,
            "routing_events": result.routing_events,
            "routing_events_dropped": result.routing_events_dropped,
            "loop_metrics": result.loop_metrics,
            "error": result.error,
            "task_success": result.outputs.get("success") is True,
            "reason": ("safety_stop" if result.status == "safety_stop" else
                       result.outputs.get("reason")),
            "env_steps": result.outputs.get("env_steps", 0),
            "task_id": task_id,
            "env_id": env_id,
            "split": split,
            "seed": seed,
            "horizon": horizon,
            "elapsed_s": round(time.perf_counter() - episode_started, 3),
            "monitor_enabled": monitor_enabled,
            "video_path": str(session_video) if session_video else None,
            "video_frame_count": getattr(session, "video_frame_count", None),
            "render_backend": os.environ.get("MUJOCO_GL"),
            "trace_dir": str(trace_dir) if trace_dir is not None else None,
            "identity": identity.to_dict(),
            "observability": source.to_dict(),
            "trace_id": str(trace_client.trace_id) if trace_client is not None else None,
            "trace_jsonl": source.trace_wal,
            "trace_sqlite": source.trace_db,
            "trace_run_id": identity.run_id,
            "metrics_dir": str(metrics_dir) if metrics_dir is not None else None,
            "metrics_wal": source.metrics_wal,
            "metrics_dropped": metrics_client.dropped if metrics_client is not None else 0,
            "dry_run": dry_run,
            "output_path": str(output_path),
            "memory_path": str(memory_path) if memory_path is not None else None,
            "ltm_summary": ltm_summary,
            "feedback_sequence": getattr(final_feedback, "sequence_no", None),
            "outputs": result.outputs,
            "context": result.context.to_dict(),
        }
        return summary
    finally:
        if live_status_path is not None and not live_terminal_written:
            _write_live_status(
                context,
                session_id=getattr(context, "session_id", None),
                status="interrupted" if context.status == "running" else str(context.status),
                error=context.error,
            )
        try:
            if supervisor is not None:
                try:
                    await supervisor.close()
                finally:
                    if environment_bound:
                        environment.restore(previous_environment)
            else:
                try:
                    if env_service is not None:
                        await env_service.close()
                finally:
                    if environment_bound:
                        environment.restore(previous_environment)
        finally:
            try:
                if app is not None:
                    await app.close()
            finally:
                try:
                    if tools is not None:
                        await tools.end_session(str(session.session_id) if session is not None else "")
                finally:
                    try:
                        await adapter.close()
                    finally:
                        await observability.close()


async def run_episode(request: EpisodeRequest, *, case: Any = None,
                      loaded: LoadedConfigs | None = None,
                      terminal: TerminalLogger | None = None) -> dict[str, Any]:
    from dataclasses import replace

    from ..task_adapters.base import TaskAdapterRunContext, TaskCase
    from .config import TaskAdapterConfig, freeze_config
    from .execution import ExecutionOptions, ResourceLease
    from .execution_registry import build_execution_backend
    from .factory import build_task_adapters
    from .observability import PortalRuntime, SharedObservability
    from .worker import WorkerRuntime

    loaded = loaded or load_request_config(request)
    params = _apply_request_overrides(loaded.params, request, case=case)
    execution = ExecutionOptions.from_params(params)
    execution.validate_tools(loaded.tools)
    lease = ResourceLease([*execution.resource_ids, *(f"gpu:{x}" for x in execution.resource_gpu_ids)], share_with_children=True)
    output = Path(request.output or params.resolve("runtime.output_path") or "data/runs/result.json").resolve()
    settings = params.resolve("runtime.web") or {}
    trace_dir = request.trace_dir or params.resolve("runtime.trace_dir")
    if not trace_dir and settings.get("enabled"):
        trace_dir = output.with_suffix(".trace")
    source = request.observability or ObservabilitySource.from_params(
        params, trace_dir=trace_dir, metrics_dir=request.metrics_dir or params.resolve("runtime.metrics_dir"))
    identity = request.identity or ExecutionIdentity.create(
        task_id=_required_param(params, "task.task_id", str), experiment_id=Path(request.config).stem)
    request = replace(request, identity=identity, output=output, observability=source,
                      config_snapshot=freeze_config(loaded),
                      memory_enabled=execution.memory_mode != "disabled")
    worker = WorkerRuntime(request, loaded=loaded, terminal=terminal)
    shared = portal = backend = None
    try:
        lease.acquire()
        if execution.max_workers != 1:
            raise ValueError("single episode execution requires max_workers=1")
        if "shared_observability" in execution.capabilities:
            if not source.trace_socket:
                shared = await SharedObservability(output.with_suffix(".observability")).start()
                source = shared.source
                request = replace(request, observability=source)
            if not source.metrics_socket:
                raise ValueError("process backend requires a shared metrics socket")
        backend = build_execution_backend(execution, request, lambda *_: request)
        if source.trace_db:
            from ..task_adapters.evaluator import write_json
            write_json(output.with_suffix(".observability.json"), source.to_dict())
        portal = PortalRuntime(source, settings)
        await portal.start()
        async def run_case(selected_case, episode_path, trace_path, memory_path):
            return await worker.run_task(request, case=selected_case)
        selected = case or TaskCase(adapter_id="single-task", task_id=identity.task_id,
                                    environment_id=_required_param(params, "task.env_id", str),
                                    index=0, seed=params.resolve("task.seed"), horizon=params.resolve("runtime.horizon"))
        adapter = build_task_adapters(TaskAdapterConfig(name="single-task"))[0]
        context = TaskAdapterRunContext(params=params, config_path=Path(request.config),
                    output_root=output.parent, run_id=identity.run_id,
                    memory_path=Path(request.memory) if request.memory else None,
                    run_episode=run_case, case_output=output, case_trace_dir=trace_dir,
                    options={"memory_mode": execution.memory_mode})
        if context.memory_path is not None and execution.memory_mode == "snapshot":
            from ..task_adapters.evaluator import write_json
            write_json(output.parent / "memory_snapshot.json", {"records": load_long_term_memory(context.memory_path, limit=None)})
        return await backend.execute(adapter, selected, context)
    finally:
        try:
            if backend is not None:
                await backend.close()
            await worker.close()
        finally:
            try:
                if portal is not None:
                    await portal.close()
                if shared is not None:
                    await shared.close()
            finally:
                lease.close()



__all__ = ["EpisodeRequest", "load_long_term_memory", "run_episode"]
