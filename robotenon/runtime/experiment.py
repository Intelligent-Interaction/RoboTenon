"""Ordered experiment execution built on the unified episode runner."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..infra.params import Params, merge_parameter_values
from ..runtime.config import freeze_config, load_request_config
from ..runtime.factory import build_task_adapters
from ..task_adapters import TaskCase, evaluate_task_adapters
from ..task_adapters.evaluator import write_json
from .execution import ExecutionOptions, ResourceLease
from .execution_registry import build_execution_backend
from .observability import (
    ExecutionIdentity,
    ObservabilitySource,
    PortalRuntime,
    SharedObservability,
)
from .runner import EpisodeRequest
from .terminal import TerminalLogger
from .worker import WorkerRuntime


@dataclass(slots=True)
class ExperimentRequest:
    """CLI/API overrides for an ordered Task Adapter experiment."""

    config: str | Path = "configs/config.json"
    experiment_config: str | Path | None = None
    model_path: str | None = None
    model_host: str | None = None
    model_port: int | None = None
    split: str | None = None
    task_set: str | None = None
    task_names: list[str] | None = None
    max_tasks: int | None = None
    num_trials: int | None = None
    seed: int | None = None
    horizon: int | None = None
    dry_run: bool | None = None
    save_videos: bool = False
    save_traces: bool = False
    video_stride: int | None = None
    video_fps: int | None = None
    camera_width: int | None = None
    camera_height: int | None = None
    output_dir: str | Path | None = None
    metrics_dir: str | Path | None = None
    memory: str | Path | None = None
    run_id: str | None = None
    task_adapter_overrides: dict[str, dict[str, Any]] | None = None
    environment_parameters: dict[str, Any] | None = None
    config_snapshot: dict[str, Any] | str | Path | None = None
    graph_id: str | None = None


def _param(params: Any, name: str, default: Any = None) -> Any:
    value = params.resolve(name)
    return default if value is None else value


def _adapter_overrides(request: ExperimentRequest) -> dict[str, dict[str, Any]]:
    explicit = request.task_adapter_overrides
    if explicit is not None:
        if not isinstance(explicit, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, Mapping)
            for key, value in explicit.items()
        ):
            raise ValueError(
                "task_adapter_overrides must map adapter ids/names to JSON objects"
            )
    values: dict[str, Any] = {}
    for key, value in (
        ("task_set", request.task_set),
        ("task_names", request.task_names),
        ("max_tasks", request.max_tasks),
        ("num_trials", request.num_trials),
        ("split", request.split),
        ("seed", request.seed),
        ("horizon", request.horizon),
    ):
        if value is not None:
            values[key] = value
    # Providers declare which historical convenience flags they consume.
    from ..task_adapters import load_builtin_task_adapters, task_adapter_registry

    load_builtin_task_adapters()
    legacy = {}
    for name in task_adapter_registry.names():
        accepted = task_adapter_registry.get(name).cli_overrides
        selected = {key: value for key, value in values.items() if key in accepted}
        if selected:
            legacy[name] = selected
    return merge_parameter_values(
        legacy,
        request.task_adapter_overrides,
    )


def validate_task_adapter_override_targets(task_adapters, overrides) -> None:
    """Reject batch overrides that cannot be applied to an enabled adapter."""
    if overrides is None:
        return
    if not isinstance(overrides, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, Mapping)
        for key, value in overrides.items()
    ):
        raise ValueError(
            "task_adapter_overrides must map adapter ids/names to JSON objects"
        )
    available = {
        key
        for spec in task_adapters.task_adapters
        if spec.enabled
        for key in (spec.id or spec.name, spec.name)
    }
    unknown = set(overrides) - available
    if unknown:
        raise ValueError(
            f"unknown task adapter override targets: {', '.join(sorted(unknown))}"
        )


def prepare_task_adapters(loaded, request: ExperimentRequest | None = None):
    """Build the exact adapter set used by a batch without starting execution."""

    from ..task_adapters import load_plugin_task_adapters

    load_plugin_task_adapters(loaded.task_adapters.plugin_task_adapters)
    overrides = _adapter_overrides(request) if request is not None else {}
    validate_task_adapter_override_targets(
        loaded.task_adapters,
        request.task_adapter_overrides if request is not None else None,
    )
    adapters = build_task_adapters(loaded.task_adapters, overrides=overrides)
    for configured in adapters:
        validate = getattr(configured.adapter, "validate", None)
        if callable(validate):
            validate(loaded.params)
    return adapters


def _options(request: ExperimentRequest, params: Any) -> dict[str, Any]:
    return {
        "save_videos": bool(
            request.save_videos or _param(params, "render.save_videos", False)
        ),
        "save_traces": bool(
            request.save_traces or _param(params, "runtime.save_traces", False)
        ),
        "video_stride": int(
            request.video_stride
            if request.video_stride is not None
            else _param(params, "render.video_stride", 1)
        ),
        "video_fps": int(
            request.video_fps
            if request.video_fps is not None
            else _param(params, "render.video_fps", 20)
        ),
        "camera_width": int(
            request.camera_width
            if request.camera_width is not None
            else _param(params, "render.camera_width", 256)
        ),
        "camera_height": int(
            request.camera_height
            if request.camera_height is not None
            else _param(params, "render.camera_height", 256)
        ),
        "output_dir": str(
            request.output_dir
            if request.output_dir is not None
            else _param(params, "runtime.output_dir", "data/runs/eval")
        ),
        "metrics_dir": (
            str(request.metrics_dir)
            if request.metrics_dir is not None
            else _param(params, "runtime.metrics_dir", None)
        ),
    }


async def run_experiment(
    request: ExperimentRequest,
    *,
    terminal: TerminalLogger | None = None,
) -> dict[str, Any]:
    """Run all enabled Task Adapters in manifest order.

    The callback passed to ``evaluate_task_adapters`` is the same
    configuration-driven ``run_episode`` used by the single-case entry.  The
    adapter, environment, body, modules, tools, monitors, and graph therefore
    come from one master config in both modes.
    """

    loaded = load_request_config(request)
    if request.graph_id is not None:
        config = merge_parameter_values(
            loaded.bundle.config, {"runtime": {"graph_id": request.graph_id}}
        )
        params = Params(
            config,
            name=loaded.params.name,
            parent_hash=loaded.params.content_hash,
        )
        bundle = replace(loaded.bundle, params=params, config=config)
        loaded = replace(loaded, params=params, bundle=bundle)
    snapshot = freeze_config(loaded)
    from .result_projection import require_episode_outputs
    from .runner import _graph_selector

    selected = _graph_selector(
        loaded,
        loaded.params,
        EpisodeRequest(config=request.config, graph_id=request.graph_id),
    )
    require_episode_outputs(loaded.graphs.graph_map()[selected])
    options = _options(request, loaded.params)
    config_path = Path(request.config).resolve()
    adapters = prepare_task_adapters(loaded, request)
    if terminal is not None:
        terminal.plan(
            config=config_path,
            graph_id=selected,
            env_id=loaded.params.resolve("runtime.environment_id"),
            adapters=[item.instance_id for item in adapters],
        )
    run_id = request.run_id or f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex}"
    output_root = Path(options["output_dir"]) / run_id

    def make_request(
        case: TaskCase,
        episode_path: Path,
        trace_dir: Path | None,
        memory_path: Path | None,
    ) -> EpisodeRequest:
        video_path = (
            episode_path.with_suffix(".mp4") if options["save_videos"] else None
        )
        metrics_dir = None
        if options.get("metrics_dir"):
            try:
                relative_episode = episode_path.relative_to(output_root)
            except ValueError:
                relative_episode = Path(episode_path.name)
            metrics_dir = Path(
                str(options["metrics_dir"])
            ) / relative_episode.with_suffix(".metrics")
        episode_request = EpisodeRequest(
            config=config_path,
            config_snapshot=snapshot,
            environment_parameters=request.environment_parameters,
            experiment_config=request.experiment_config,
            task_id=case.task_id,
            env_id=case.environment_id,
            split=case.parameters.get("split", request.split),
            seed=case.seed,
            horizon=case.horizon,
            model_path=request.model_path,
            model_host=request.model_host,
            model_port=request.model_port,
            dry_run=request.dry_run,
            output=episode_path,
            video_path=video_path,
            video_stride=options["video_stride"],
            video_fps=options["video_fps"],
            camera_width=options["camera_width"],
            camera_height=options["camera_height"],
            trace_dir=trace_dir,
            metrics_dir=metrics_dir,
            live_status_path=episode_path.parent
            / ".live"
            / f"{episode_path.stem}.json",
            memory=memory_path,
            graph_id=request.graph_id,
            identity=ExecutionIdentity.create(
                task_id=case.task_id,
                experiment_id=config_path.stem,
                run_id=run_id,
                episode_id=f"{case.adapter_id}:{case.index}:{case.episode}",
                adapter_id=case.adapter_id,
            ),
        )
        episode_request.observability = source
        episode_request.memory_enabled = execution.memory_mode != "disabled"
        return episode_request

    async def run_case(case, episode_path, trace_dir, memory_path):
        return await worker.run_task(make_request(case, episode_path, trace_dir, memory_path), case=case)

    execution = ExecutionOptions.from_params(loaded.params)
    execution.validate_tools(loaded.tools)
    options["memory_mode"] = execution.memory_mode
    options["enumeration_parallelism"] = execution.enumeration_parallelism
    profile = EpisodeRequest(config=config_path, experiment_config=request.experiment_config,
                             config_snapshot=snapshot,
                             output=output_root / "worker-profile.json",
                             model_path=request.model_path, model_host=request.model_host,
                             model_port=request.model_port, dry_run=request.dry_run,
                             graph_id=request.graph_id)
    worker = WorkerRuntime(profile, loaded=loaded, terminal=terminal)
    shared = None
    portal = None
    lease = ResourceLease([*execution.resource_ids, *(f"gpu:{x}" for x in execution.resource_gpu_ids)], share_with_children=True)
    source = None
    backend = build_execution_backend(execution, profile, make_request)
    memory_path = Path(request.memory) if request.memory else None
    from .run_state import RunReservation
    reservation = RunReservation(options["output_dir"], run_id, loaded.params.content_hash).acquire()
    try:
        write_json(output_root / "config.snapshot.json", snapshot)
        lease.acquire()
        # Source selection is a launch concern, never a query-side mode switch.
        configured_source = ObservabilitySource.from_params(loaded.params)
        if "shared_observability" in execution.capabilities and not configured_source.trace_socket:
            if configured_source.trace_db or configured_source.trace_wal or configured_source.metrics_socket:
                raise ValueError("process backend requires both shared sockets or launcher-owned observability")
            shared = await SharedObservability(output_root / "observability").start()
            source = shared.source
        elif any(configured_source.to_dict().values()):
            source = configured_source
        web_settings = loaded.params.resolve("runtime.web") or {}
        if source is None and web_settings.get("enabled"):
            root = (output_root / "observability").resolve()
            source = ObservabilitySource(
                trace_db=str(root / "trace.sqlite3"),
                trace_wal=str(root / "trace.jsonl"),
                metrics_wal=str(root / "metrics.jsonl"),
            )
        if "shared_observability" in execution.capabilities and (
            not source.trace_socket or not source.metrics_socket
        ):
            raise ValueError(
                "process backend requires shared trace and metrics sockets"
            )
        if source is not None:
            write_json(output_root / "observability.json", source.to_dict())
        portal = PortalRuntime(source, web_settings)
        await portal.start()
        return await evaluate_task_adapters(
            _reservation=reservation,
            config_path=config_path,
            params=loaded.params,
            adapters=adapters,
            output_dir=options["output_dir"],
            run_id=run_id,
            run_episode=run_case,
            execution_backend=backend,
            memory_path=memory_path,
            options=options,
            on_case_start=terminal.case_start if terminal is not None else None,
            on_case_complete=terminal.case_complete if terminal is not None else None,
            on_case_reset=shared.trace_store.delete_episode
            if shared is not None
            else None,
        )
    finally:
        try:
            await backend.close()
        finally:
            try:
                await worker.close()
            finally:
                try:
                    if portal is not None:
                        await portal.close()
                    if shared is not None:
                        await shared.close()
                finally:
                    lease.close()
                    reservation.close()


__all__ = [
    "ExperimentRequest",
    "prepare_task_adapters",
    "run_experiment",
    "validate_task_adapter_override_targets",
]
