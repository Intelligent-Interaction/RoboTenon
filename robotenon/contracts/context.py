"""Serializable run state, independent of graph execution and transports."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Literal
from uuid import UUID

from ..schemas import (
    Action,
    ActionChunk,
    BodyExecutionResult,
    EnvironmentFeedback,
    EvaluationDecision,
    MonitorReport,
    SafetyStateSnapshot,
    TaskItem,
)
from .checkpoint import RunCheckpoint
from .execution import current_frame
from .serialization import jsonable as _jsonable
from .workspace import (
    BUSINESS_FIELDS,
    FrozenMap,
    current_trigger,
    current_view,
    current_workspace,
    freeze,
    thaw,
)

GraphStatus = Literal[
    "running", "completed", "failed", "max_steps", "safety_stop", "recovery_required"
]


@dataclass(slots=True)
class GraphContext:
    """Run-scoped global context passed unchanged to every graph module.

    ``values`` is a task-local, lifecycle-checked draft during graph execution.
    ``inputs`` and ``results`` expose recursively immutable published data.
    ``outputs`` keeps the last diagnostic return value of each node for
    conditions such as ``ctx.outputs['grasp']['ok'] == true``. ``params`` is
    the immutable configuration view supplied by the Parameter module;
    environment access is provided by the process singleton ``ctx.env``/
    ``robotenon.envs.environment`` rather than a module argument.
    """

    _values_lock: Any = field(default_factory=RLock, init=False, repr=False, compare=False)
    values: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    node_records: dict[str, Any] = field(default_factory=dict)
    root_scope: str | None = None
    _running: bool = field(default=False, init=False, repr=False, compare=False)
    _managed: bool = field(default=False, init=False, repr=False, compare=False)
    _pending_publications: dict = field(default_factory=dict, init=False, repr=False, compare=False)
    session_id: Any = None
    task_id: str | None = None
    task_item: TaskItem | None = None
    feedback: EnvironmentFeedback | None = None
    action: Action | None = None
    action_chunk: ActionChunk | None = None
    body_result: BodyExecutionResult | None = None
    monitor_report: MonitorReport | None = None
    decision: EvaluationDecision | None = None
    safety: SafetyStateSnapshot = field(default_factory=SafetyStateSnapshot)
    outputs: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    # Node history is a diagnostic window, not the durable event log.  Keep
    # it bounded so long-running loops do not make every checkpoint larger;
    # Recorder/Trace remain the complete history sinks.
    history_limit: int = 2048
    history_dropped: int = 0
    # Scoped outputs include one entry per graph path/activation.  They are
    # useful for debugging nested graphs but must not grow without bound in a
    # long-running loop.
    scoped_outputs_limit: int = 2048
    scoped_outputs_dropped: int = 0
    step: int = 0
    current_node: str | None = None
    current_graph: str | None = None
    # Durable tail-transition state. ``active_graph`` identifies the graph
    # whose root cursor must resume; ``transition_pending`` means its old
    # cursor still needs to be reset before entering from the target's entry.
    active_graph: str | None = None
    transition_pending: bool = False
    transition_inputs: dict[str, Any] = field(default_factory=dict)
    status: GraphStatus = "running"
    error: str | None = None
    graph_stack: list[str] = field(default_factory=list)
    emergency_history: list[str] = field(default_factory=list)
    recovery_history: list[str] = field(default_factory=list)
    # Monotonic revision assigned by the optional cross-process context store.
    # It is separate from ``step`` because one graph step may produce several
    # service-side observations or checkpoints.
    context_revision: int = 0
    checkpoint_version: int = 1
    graph_fingerprint: str | None = None
    cursors: dict[str, Any] = field(default_factory=dict)
    scoped_outputs: dict[str, Any] = field(default_factory=dict)
    # Live resident ToolRuntime injected by GraphRuntime. The handle is
    # process-local and deliberately omitted from checkpoints.
    tools: Any = field(default=None, repr=False, compare=False)
    # Immutable per-experiment Params handle. It is process-local and omitted
    # from checkpoints; the snapshot hash/config is persisted by the caller.
    params: Any = field(default=None, repr=False, compare=False)
    # Optional live observability handles. They are intentionally excluded
    # from checkpoints; Metrics/Trace have their own durable sinks.
    metrics: Any = field(default=None, repr=False, compare=False)
    trace: Any = field(default=None, repr=False, compare=False)
    # Raw per-environment-step observations are a live hand-off from the
    # local environment executor to resident policy Tools.  They intentionally
    # stay out of checkpoints because ObservationFrame.data may contain numpy
    # buffers; the VLA consumes and clears this queue before the next plan.
    observation_updates: list[EnvironmentFeedback] = field(
        default_factory=list, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Validate the small pieces of execution state we persist.

        The extension dictionaries intentionally remain dynamic, but malformed
        counters or status values would make a restored scheduler ambiguous.
        Fail at the contract boundary instead of much later in a graph wave.
        """

        if self.history_limit < 1:
            raise ValueError("history_limit must be positive")
        if self.scoped_outputs_limit < 1:
            raise ValueError("scoped_outputs_limit must be positive")
        for name in (
            "history_dropped",
            "scoped_outputs_dropped",
            "step",
            "context_revision",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.checkpoint_version != 1:
            raise ValueError(
                f"unsupported checkpoint version: {self.checkpoint_version}"
            )
        if self.status not in {
            "running",
            "completed",
            "failed",
            "max_steps",
            "safety_stop",
            "recovery_required",
        }:
            raise ValueError(f"unsupported graph status: {self.status!r}")
        if self.active_graph is not None and not isinstance(self.active_graph, str):
            raise TypeError("active_graph must be a string or null")
        if not isinstance(self.transition_pending, bool):
            raise TypeError("transition_pending must be boolean")
        if not isinstance(self.transition_inputs, dict):
            raise TypeError("transition_inputs must be an object")

    def append_history(self, event: dict[str, Any]) -> None:
        """Append one diagnostic event while enforcing the bounded window."""

        if self.history_limit < 1:
            raise ValueError("history_limit must be positive")
        history = object.__getattribute__(self, "history")
        if len(history) >= self.history_limit:
            history.pop(0)
            object.__setattr__(self, "history_dropped", self.history_dropped + 1)
        history.append(event)

    def record_scoped_output(self, path: str, value: Any) -> None:
        """Record one path-qualified node output within a bounded window."""

        if self.scoped_outputs_limit < 1:
            raise ValueError("scoped_outputs_limit must be positive")
        if path in self.scoped_outputs:
            self.scoped_outputs.pop(path, None)
        elif len(self.scoped_outputs) >= self.scoped_outputs_limit:
            oldest = next(iter(self.scoped_outputs), None)
            if oldest is not None:
                self.scoped_outputs.pop(oldest, None)
                self.scoped_outputs_dropped += 1
        self.scoped_outputs[path] = value

    @property
    def env(self) -> Any:
        """Return the process-resident environment handle.

        Environment is not stored in the run context and is never passed as a
        module argument.  This compatibility property gives older extensions
        a migration path while ensuring they see the singleton proxy rather
        than a second per-module connection.
        """

        from ..envs.runtime import environment

        return environment

    @env.setter
    def env(self, value: Any) -> None:
        """Compatibility setter that binds the process singleton."""

        if value is None:
            return
        from ..envs.runtime import bind_environment, environment

        if value is not environment:
            bind_environment(value)

    def __setattr__(self, name, value):
        if name in BUSINESS_FIELDS:
            workspace = current_workspace(self)
            if workspace is not None:
                workspace.set_field(name, value)
                return
        if not name.startswith("_") and current_workspace(self) is not None:
            raise AttributeError(f"cannot replace framework-owned ctx.{name}")
        object.__setattr__(self, name, value)

    @property
    def trigger(self):
        """Invocation source, task-local and also available in route conditions."""
        return current_trigger(self)

    @property
    def inputs(self):
        workspace = current_workspace(self)
        if workspace is not None:
            workspace.check()
            return workspace.inputs
        return freeze(object.__getattribute__(self, "values"))

    def _result_records(self):
        workspace = current_workspace(self)
        if workspace is not None:
            workspace.check()
            return workspace.records
        view = current_view(self)
        if view is not None:
            return view[2]
        return freeze(object.__getattribute__(self, "node_records").get(self.root_scope, {}))

    @property
    def results(self):
        return FrozenMap({name: record["values"] for name, record in self._result_records().items()})

    @property
    def result_versions(self):
        return FrozenMap({name: record["version"] for name, record in self._result_records().items()})

    @property
    def previous(self):
        workspace = current_workspace(self)
        if workspace is None:
            raise RuntimeError("ctx.previous requires a node workspace")
        workspace.check()
        return workspace.previous

    def read_values(self, *nodes):
        """Detached inputs plus explicitly named results, oldest version first.

        This migration helper never guesses which other nodes to read.
        """
        values = thaw(self.inputs)
        records = self._result_records()
        for name in sorted((n for n in nodes if n in records), key=lambda n: records[n]["version"]):
            values.update(thaw(records[name]["values"]))
        return values

    def __getattribute__(self, name):
        if name == "values":
            workspace = current_workspace(self)
            if workspace is not None:
                workspace.check()
                return workspace.values
            if object.__getattribute__(self, "_managed"):
                raise RuntimeError("ctx.values is node-private; use ctx.inputs or ctx.results outside a node")
        if name in BUSINESS_FIELDS:
            workspace = current_workspace(self)
            if workspace is not None:
                return workspace.get_field(name)
        if name in {"outputs", "node_records", "cursors", "history", "scoped_outputs", "emergency_history", "recovery_history"}:
            workspace = current_workspace(self)
            if workspace is not None:
                workspace.check()
                return workspace.outputs if name == "outputs" else freeze(object.__getattribute__(self, name))
        # Preserve the public shared ctx identity while giving each asyncio
        # branch its own diagnostic position. Outside a node, return the last
        # completed position for compatibility with existing callers.
        if name in {"current_node", "current_graph", "graph_stack"}:
            frame = current_frame(self)
            if frame is not None:
                if name == "current_node":
                    return frame.node
                if name == "current_graph":
                    return frame.graphs[-1]
                return list(frame.graphs)
        return object.__getattribute__(self, name)

    def snapshot_values(self, *keys: str) -> dict[str, Any]:
        """Detached, consistent values for readers using the cooperative API."""
        workspace = current_workspace(self)
        with workspace.lock if workspace is not None else self._values_lock:
            return deepcopy({key: self.values[key] for key in keys} if keys else self.values)

    def atomic_update(self, key: str, updater, *, default: Any = None) -> Any:
        """Atomically read/modify/write one key from asyncio tasks or threads.

        The synchronous updater receives a detached value and must not await
        or perform slow I/O. Exceptions leave the value unchanged. All writers
        of the same data must use this API; raw nested mutations bypass it.
        """
        workspace = current_workspace(self)
        with workspace.lock if workspace is not None else self._values_lock:
            value = updater(deepcopy(self.values.get(key, default)))
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError("atomic_update updater must be synchronous")
            self.values[key] = deepcopy(value)
            return value

    def update(self, values: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        workspace = current_workspace(self)
        with workspace.lock if workspace is not None else self._values_lock:
            if values:
                self.values.update(values)
            if kwargs:
                self.values.update(kwargs)

    def record_output(
        self, node_id: str, output: Any, *, merge_values: bool = True
    ) -> None:
        workspace = current_workspace(self)
        if workspace is not None:
            workspace.output = deepcopy(output)
        else:
            self.outputs[node_id] = output
        if isinstance(output, Action):
            self.action = output
            self.action_chunk = None
        elif isinstance(output, ActionChunk):
            self.action_chunk = output
            if output.actions:
                self.action = output.actions[0]
        elif isinstance(output, BodyExecutionResult):
            self.body_result = output
            if output.feedback is not None:
                self.feedback = output.feedback
        elif isinstance(output, EnvironmentFeedback):
            self.feedback = output
            if self.session_id is None:
                self.session_id = output.session_id
        elif isinstance(output, MonitorReport):
            self.monitor_report = output
            self.safety = (
                self.safety.copy(update={"level": output.safety_level})
                if hasattr(self.safety, "copy")
                else self.safety
            )
        elif isinstance(output, EvaluationDecision):
            self.decision = output
        if isinstance(output, Mapping):
            if not merge_values:
                return
            self.values.update(output)
            action = output.get("action")
            action_chunk = output.get("action_chunk")
            body_result = output.get("body_result")
            feedback = output.get("feedback")
            monitor_report = output.get("monitor_report")
            decision = output.get("decision")
            if isinstance(action, Action):
                self.action = action
                if not isinstance(action_chunk, ActionChunk):
                    self.action_chunk = None
            if isinstance(action_chunk, ActionChunk):
                self.action_chunk = action_chunk
                if action_chunk.actions:
                    self.action = action_chunk.actions[0]
            if isinstance(body_result, BodyExecutionResult):
                self.body_result = body_result
                if body_result.feedback is not None:
                    self.feedback = body_result.feedback
            if isinstance(feedback, EnvironmentFeedback):
                self.feedback = feedback
                if self.session_id is None:
                    self.session_id = feedback.session_id
            if isinstance(monitor_report, MonitorReport):
                self.monitor_report = monitor_report
            if isinstance(decision, EvaluationDecision):
                self.decision = decision

    def module_context(self):
        from .module import ModuleContext

        return ModuleContext(
            feedback=self.feedback,
            task=self.task_item,
            values=self.values,
            tools=self.tools,
            params=self.params,
            metrics=self.metrics,
            trace=self.trace,
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ):
        """Call a resident tool while retaining this shared context."""

        if self.tools is None:
            raise RuntimeError(
                "GraphContext has no ToolRuntime; inject tools into GraphRuntime"
            )
        return await self.tools.call(name, arguments, context=self)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible snapshot without live runtime handles.

        ``env``, ``tools`` and ``params`` intentionally never appear in this
        object. Environment is a process-resident singleton; tools and params
        are runtime handles owned by the bootstrap layer and are recreated or
        injected outside the serializable graph state.
        """
        return {
            "values": _jsonable(object.__getattribute__(self, "values")),
            "node_records": _jsonable(object.__getattribute__(self, "node_records")),
            "root_scope": self.root_scope,
            "workspace_version": 1,
            "session_id": _jsonable(object.__getattribute__(self, "session_id")),
            "task_id": object.__getattribute__(self, "task_id"),
            "task_item": _jsonable(object.__getattribute__(self, "task_item")),
            "feedback": _jsonable(object.__getattribute__(self, "feedback")),
            "action": _jsonable(object.__getattribute__(self, "action")),
            "action_chunk": _jsonable(object.__getattribute__(self, "action_chunk")),
            "body_result": _jsonable(object.__getattribute__(self, "body_result")),
            "monitor_report": _jsonable(object.__getattribute__(self, "monitor_report")),
            "decision": _jsonable(object.__getattribute__(self, "decision")),
            "safety": _jsonable(object.__getattribute__(self, "safety")),
            "outputs": _jsonable(object.__getattribute__(self, "outputs")),
            "history": _jsonable(self.history),
            "history_limit": self.history_limit,
            "history_dropped": self.history_dropped,
            "scoped_outputs_limit": self.scoped_outputs_limit,
            "scoped_outputs_dropped": self.scoped_outputs_dropped,
            "step": self.step,
            "current_node": self.current_node,
            "current_graph": self.current_graph,
            "active_graph": self.active_graph,
            "transition_pending": self.transition_pending,
            "transition_inputs": _jsonable(self.transition_inputs),
            "status": self.status,
            "error": self.error,
            "graph_stack": [],  # Live stacks are task-local, never restored.
            "emergency_history": list(self.emergency_history),
            "recovery_history": list(self.recovery_history),
            "context_revision": self.context_revision,
            "checkpoint_version": self.checkpoint_version,
            "graph_fingerprint": self.graph_fingerprint,
            "cursors": _jsonable(self.cursors),
            "scoped_outputs": _jsonable(self.scoped_outputs),
        }

    def to_json(self, **kwargs: Any) -> str:
        """Serialize the context for checkpoints or cross-process handoff."""
        kwargs.setdefault("ensure_ascii", False)
        kwargs.setdefault("allow_nan", False)
        return json.dumps(_jsonable(self.to_dict()), **kwargs)

    # Pydantic-style spellings make checkpoint integrations agnostic to the
    # fact that GraphContext is a mutable dataclass rather than a BaseModel.
    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self.to_dict()

    def model_dump_json(self, **kwargs: Any) -> str:
        return self.to_json(**kwargs)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GraphContext:
        """Restore a context snapshot produced by :meth:`to_dict`.

        Unknown values remain in the extension dictionaries so a checkpoint
        can round-trip application-specific state without a schema migration.
        """
        version = int(value.get("checkpoint_version", 1))
        if version != 1:
            raise ValueError(f"unsupported checkpoint version: {version}")
        if value.get("graph_stack") and not value.get("cursors"):
            raise ValueError(
                "legacy in-flight checkpoint has no execution cursor; explicit restart required"
            )
        context = cls(
            values=dict(value.get("values") or {}),
            session_id=_restore_uuid(value.get("session_id")),
            task_id=value.get("task_id"),
            task_item=_restore_model(TaskItem, value.get("task_item")),
            feedback=_restore_model(EnvironmentFeedback, value.get("feedback")),
            action=_restore_model(Action, value.get("action")),
            action_chunk=_restore_model(ActionChunk, value.get("action_chunk")),
            body_result=_restore_model(BodyExecutionResult, value.get("body_result")),
            monitor_report=_restore_model(MonitorReport, value.get("monitor_report")),
            decision=_restore_model(EvaluationDecision, value.get("decision")),
            safety=_restore_model(SafetyStateSnapshot, value.get("safety"))
            or SafetyStateSnapshot(),
            outputs=dict(value.get("outputs") or {}),
            history=list(value.get("history") or []),
            # Do not silently repair malformed limits/counters while loading
            # a checkpoint.  __post_init__ validates the contract and gives
            # operators a precise error instead of allowing a corrupted
            # snapshot to resume with a different memory bound.
            history_limit=int(value.get("history_limit", 2048)),
            history_dropped=int(value.get("history_dropped", 0)),
            scoped_outputs_limit=int(value.get("scoped_outputs_limit", 2048)),
            scoped_outputs_dropped=int(value.get("scoped_outputs_dropped", 0)),
            step=int(value.get("step", 0)),
            current_node=value.get("current_node"),
            current_graph=value.get("current_graph"),
            active_graph=value.get("active_graph"),
            transition_pending=value.get("transition_pending", False),
            transition_inputs=dict(value.get("transition_inputs") or {}),
            status=value.get("status", "running"),
            error=value.get("error"),
            graph_stack=[],
            emergency_history=list(value.get("emergency_history") or []),
            recovery_history=list(value.get("recovery_history") or []),
            context_revision=int(value.get("context_revision", 0)),
            checkpoint_version=version,
            graph_fingerprint=value.get("graph_fingerprint"),
            cursors=dict(value.get("cursors") or {}),
            scoped_outputs=dict(value.get("scoped_outputs") or {}),
        )
        if value.get("workspace_version", 1) != 1:
            raise ValueError("unsupported workspace checkpoint version")
        records_by_scope = value.get("node_records") or {}
        if not isinstance(records_by_scope, Mapping):
            raise TypeError("checkpoint node_records must be an object")
        for scope, records in records_by_scope.items():
            scope_path = json.loads(scope)
            if not isinstance(scope_path, list) or not all(isinstance(part, str) for part in scope_path):
                raise ValueError("invalid result scope")
            if not isinstance(records, Mapping):
                raise TypeError("checkpoint result scope must be an object")
            for node, record in records.items():
                if not isinstance(record, Mapping):
                    raise TypeError("checkpoint result must be an object")
                version = record.get("version")
                if type(version) is not int or version < 1:
                    raise ValueError("invalid node result version")
                if record.get("path") != [*scope_path, node, str(version)]:
                    raise ValueError("node result path/version mismatch")
                if not isinstance(record.get("values"), Mapping):
                    raise TypeError("node result values must be an object")
        context.node_records = {scope: dict(freeze(records)) for scope, records in records_by_scope.items()}
        context.root_scope = value.get("root_scope")
        context._managed = bool(context.root_scope)
        if context.cursors and value.get("workspace_version") != 1:
            raise ValueError("legacy in-flight checkpoint requires explicit restart for private values")
        return context

    def to_checkpoint(self) -> RunCheckpoint:
        """Create the explicit versioned checkpoint contract for this run."""

        return RunCheckpoint.from_context(self)

    @classmethod
    def from_checkpoint(cls, checkpoint: RunCheckpoint | Mapping[str, Any]) -> GraphContext:
        """Restore a context from a :class:`RunCheckpoint` or its mapping."""

        value = (
            checkpoint.state
            if isinstance(checkpoint, RunCheckpoint)
            else RunCheckpoint.from_dict(dict(checkpoint)).state
        )
        return cls.from_dict(value)


def _restore_uuid(value: Any) -> UUID | str | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _restore_model(model: Any, value: Any) -> Any:
    if value is None or isinstance(value, model):
        return value
    try:
        validator = getattr(model, "model_validate", None)
        return validator(value) if callable(validator) else model.parse_obj(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid checkpoint field for {model.__name__}: {exc}"
        ) from exc
