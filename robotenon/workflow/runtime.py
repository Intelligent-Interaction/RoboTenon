"""Execution of JSON graphs with one shared mutable context."""

from __future__ import annotations

from ..contracts.persistence import SafetyStore

import asyncio
import inspect
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..contracts.context import GraphContext, GraphStatus
from ..contracts.execution import (
    ExecutionFrame,
    claim_patch,
    current_frame,
    execution_frame,
)
from ..contracts.module import NodeResult
from ..contracts.workspace import (
    Workspace,
    bind_workspace,
    current_view,
    current_workspace,
    freeze,
    result_view,
)
from ..core.context import ContextStore, get_default_context_store
from ..envs.base.protocol import EnvironmentSession
from ..envs.runtime import environment
from ..infra.trace import trace_observation, trace_value
from ..modules import ModuleResult, ModuleRuntime
from ..modules.base import invoke_graph_module
from ..runtime.safety import (
    GLOBAL_SAFETY_STATE,
    async_reconcile_safety_snapshot,
    async_safety_call,
)
from ..schemas import (
    EvaluationDecision,
    SafetyLevel,
)
from ..services.protocol import BodyService, EnvironmentService, MonitorService
from ..tools import ToolRuntime
from .compiler import compile_graphs
from .conditions import evaluate_condition
from .definition import GraphDefinition, GraphNode
from .operations import ServiceOperations
from .result import GraphResult, GraphTransitionRequest
from .bindings import resolve_bindings, graph_outputs
from .scheduler import WaveScheduler


class GraphRuntime:
    """Run named graph definitions using resident modules and service clients.

    The environment is a process-resident singleton, not a Module input.
    Applications should bind an EnvironmentSession/EnvironmentService once
    with ``robotenon.envs.bind_environment``.  Modules can then use the same
    environment service without receiving it as an input. ``body`` is the
    resident cerebellum client and ``monitor`` is the independent
    compliance-monitor client. ``tools`` is the resident capability runtime
    used by Modules for model, memory and skill calls. A graph node receives
    exactly one argument, ``run(ctx)``; it reads merged parameters from
    ``ctx.params``. Nested graphs share the same context and service handles;
    no second environment/body/tool connection is created.
    """

    def __init__(
        self,
        graphs: Mapping[str, GraphDefinition] | list[GraphDefinition],
        modules: ModuleRuntime,
        *,
        backend: str = "langgraph",
        max_depth: int = 16,
        safety_state: SafetyStore = GLOBAL_SAFETY_STATE,
        stop_on_unhandled_safety: bool = True,
        context_store: ContextStore | None = None,
        safety_gate=None,
        tools: ToolRuntime | None = None,
        params: Any = None,
        metrics: Any = None,
        trace: Any = None,
        event_callback: Any = None,
    ) -> None:
        self.graphs = (
            {graph.id: graph for graph in graphs}
            if not isinstance(graphs, Mapping)
            else dict(graphs)
        )
        self.modules = modules
        # Explicit injection wins; otherwise reuse the ToolRuntime attached
        # to ModuleRuntime.  Both references point to the same resident
        # instances for nested graphs and every run.
        self.tools = tools if tools is not None else getattr(modules, "tools", None)
        self.params = params if params is not None else getattr(modules, "params", None)
        self.metrics = metrics
        self.trace = trace
        self.event_callback = event_callback
        if self.tools is not None:
            bind_tools = getattr(self.modules, "bind_tools", None)
            if callable(bind_tools):
                bind_tools(self.tools)
            if self.trace is not None:
                set_trace = getattr(self.tools, "set_trace", None)
                if callable(set_trace):
                    set_trace(self.trace)
            if self.metrics is not None:
                set_metrics = getattr(self.tools, "set_metrics", None)
                if callable(set_metrics):
                    set_metrics(self.metrics)
        self.backend = backend.lower()
        self.max_depth = max_depth
        self.safety_state = safety_state
        self.stop_on_unhandled_safety = stop_on_unhandled_safety
        self.operations = ServiceOperations(safety_state, safety_gate)
        self.context_store = (
            context_store if context_store is not None else get_default_context_store()
        )
        # A wave can finish several sibling nodes at once.  Serialise their
        # CAS checkpoint commits so concurrent worker threads do not all use
        # the same expected revision and turn a healthy parallel wave into a
        # false context-conflict failure.
        self._checkpoint_lock = asyncio.Lock()
        # Claim a context atomically before restoring or mutating it.  The
        # previous set-only check had an await between the check and insert,
        # allowing two callers to enter the same run concurrently.
        self._active_context_lock = asyncio.Lock()
        self._active_contexts: set[int] = set()
        self._context_leases = {}
        self._emergency_locks = {}
        if not self.graphs:
            raise ValueError("GraphRuntime requires at least one graph")
        if self.backend not in {"langgraph", "fallback"}:
            raise ValueError("backend must be 'langgraph' or 'fallback'")
        self.compiled, self.fingerprint = compile_graphs(
            self.graphs, self.modules, self.tools
        )

    async def _emit_event(
        self,
        ctx: GraphContext,
        event: dict[str, Any],
        result: GraphResult | None = None,
    ) -> None:
        """Publish optional live execution events without affecting execution."""

        if self.event_callback is None:
            return
        try:
            value = self.event_callback(ctx, event, result)
            if inspect.isawaitable(value):
                await value
        except Exception:
            # Live state is diagnostic output. A broken writer must never turn
            # a valid graph execution into a failed experiment.
            return

    async def run(
        self,
        graph_id: str,
        ctx: GraphContext | None = None,
        *,
        body: BodyService | None = None,
        monitor: MonitorService | None = None,
        context: GraphContext | None = None,
        initial_values: Mapping[str, Any] | None = None,
        params: Any = None,
    ) -> GraphResult:
        """Execute one graph with the resident environment and service clients.

        ``context=`` is accepted as a spelling-compatible alias for ``ctx``;
        callers should use ``ctx=`` in new code.  The environment must already
        be bound during bootstrap with ``bind_environment(service)``.  The
        runtime never creates, temporarily rebinds, or closes the environment
        service.  Nested graphs receive the exact same ``ctx``, process-
        resident environment handle, ``body`` and ``monitor`` objects.
        """
        if ctx is not None and context is not None:
            raise TypeError("pass either ctx or context, not both")
        if graph_id not in self.graphs:
            raise KeyError(
                f"unknown graph {graph_id!r}; available: {', '.join(sorted(self.graphs))}"
            )
        if self.tools is not None:
            await self.tools.start()
        await self.modules.start()
        # Do not use truthiness here: callers may provide a custom
        # GraphContext subclass whose ``__bool__`` is false.  The exact object
        # supplied by the caller is the global context for every graph switch.
        run_ctx = (
            ctx
            if ctx is not None
            else (context if context is not None else GraphContext())
        )
        context_key = await self._claim_context(run_ctx)
        try:
            # ``tools`` is a live runtime handle, not durable user state.  It is
            # reattached on every run (and after checkpoint restoration).
            run_ctx.tools = self.tools
            run_ctx.params = (
                params
                if params is not None
                else self.params
                if self.params is not None
                else run_ctx.params
            )
            run_ctx.metrics = self.metrics
            run_ctx.trace = self.trace
            await self._restore_context_if_available(run_ctx)
            if initial_values:
                if run_ctx.cursors and run_ctx.status != "completed":
                    raise ValueError("cannot change inputs while resuming a graph")
                object.__getattribute__(run_ctx, "values").update(deepcopy(initial_values))
            object.__setattr__(run_ctx, "values", deepcopy(object.__getattribute__(run_ctx, "values")))
            run_ctx._managed = True
            if run_ctx.cursors and run_ctx.graph_fingerprint != self.fingerprint:
                raise ValueError(
                    "checkpoint graph fingerprint differs; explicit restart required"
                )
            if run_ctx.status == "completed":
                run_ctx.cursors.clear()
                run_ctx.active_graph = None
                run_ctx.transition_pending = False
                run_ctx.transition_inputs.clear()
            active_graph = run_ctx.active_graph or graph_id
            if active_graph not in self.graphs:
                raise ValueError(f"transition target graph {active_graph!r} is unknown")
            run_ctx.active_graph = active_graph
            run_ctx.root_scope = json.dumps([active_graph])
            run_ctx.graph_fingerprint = self.fingerprint
            run_ctx.status = "running"
            run_ctx.error = None
            run_ctx.safety = await async_safety_call(self.safety_state, "snapshot")
            try:
                effective_env = environment.current
                while True:
                    active_graph = run_ctx.active_graph or graph_id
                    if run_ctx.transition_pending:
                        prefix = (active_graph,)
                        for scope in list(run_ctx.cursors):
                            path = tuple(json.loads(scope))
                            if path[:1] == prefix:
                                run_ctx.cursors.pop(scope, None)
                        object.__getattribute__(run_ctx, "values").update(
                            deepcopy(run_ctx.transition_inputs)
                        )
                        run_ctx.transition_inputs.clear()
                        run_ctx.transition_pending = False
                        run_ctx.root_scope = json.dumps([active_graph])
                        await self._persist_context_async(run_ctx)
                    result = await self._run_graph(
                        active_graph, run_ctx, effective_env, body, monitor
                    )
                    if isinstance(result, GraphTransitionRequest):
                        continue
                    break
            except asyncio.CancelledError:
                # Cancellation is a normal supervisor event, not a failure
                # that should be swallowed.  Persist the latest resumable
                # cursor before re-raising so a restarted process can resume
                # from the last committed wave boundary.
                run_ctx.status = "running"
                try:
                    await asyncio.shield(self._persist_context_async(run_ctx))
                except BaseException:
                    # The cancellation must remain observable even if storage
                    # is unavailable; the next run can report the missing
                    # checkpoint explicitly.
                    pass
                raise
            except Exception as exc:
                result = self._failure(graph_id, run_ctx, str(exc), "failed")

            run_ctx.status = result.status
            run_ctx.error = result.error
            if result.status == "completed":
                run_ctx.active_graph = None
                run_ctx.transition_pending = False
                run_ctx.transition_inputs.clear()
            try:
                await self._persist_context_async(run_ctx)
            except Exception as exc:
                result = self._failure(graph_id, run_ctx, str(exc), "failed")
            return result
        finally:
            await self._release_context(context_key)
            self._emergency_locks.pop(id(run_ctx), None)

    async def _claim_context(self, ctx: GraphContext) -> int:
        key = id(ctx)
        async with self._active_context_lock:
            with ctx._values_lock:
                if key in self._active_contexts or ctx._running:
                    raise RuntimeError("this context is already running")
                ctx._running = True
                self._context_leases[key] = ctx
                self._active_contexts.add(key)
        return key

    async def _release_context(self, key: int) -> None:
        async with self._active_context_lock:
            ctx = self._context_leases.pop(key, None)
            if ctx is not None:
                with ctx._values_lock:
                    ctx._running = False
                    ctx._pending_publications.clear()
            self._active_contexts.discard(key)

    async def recover(
        self,
        graph_id: str,
        ctx: GraphContext,
        *,
        body: BodyService | None = None,
        monitor: MonitorService | None = None,
        approved: bool = False,
    ) -> GraphResult:
        """Recover a stopped run according to the graph's level policy.

        Recovery is intentionally explicit and separate from :meth:`run`:
        ``auto`` policies may be called by a supervisor, while ``manual``
        policies require ``approved=True`` after a human/hardware
        acknowledgement.  The configured ``recovery_graph`` and optional
        ``resume_graph`` receive the exact same ``ctx`` and service clients;
        no environment session or body connection is recreated.
        """
        context_key = await self._claim_context(ctx)
        try:
            return await self._recover_impl(
                graph_id,
                ctx,
                body=body,
                monitor=monitor,
                approved=approved,
            )
        finally:
            await self._release_context(context_key)

    async def _recover_impl(
        self,
        graph_id: str,
        ctx: GraphContext,
        *,
        body: BodyService | None = None,
        monitor: MonitorService | None = None,
        approved: bool = False,
    ) -> GraphResult:
        if graph_id not in self.graphs:
            raise KeyError(
                f"unknown graph {graph_id!r}; available: {', '.join(sorted(self.graphs))}"
            )
        # Recovery may be invoked by a supervisor without a preceding
        # ``run()`` call (for example after a process restart).  Ensure the
        # resident capability and module lifecycles are ready before entering
        # a recovery graph.
        if self.tools is not None:
            await self.tools.start()
        await self.modules.start()
        graph = self.graphs[graph_id]
        snapshot = await async_safety_call(self.safety_state, "snapshot")
        object.__setattr__(ctx, "safety", snapshot)
        policy = graph.recovery_for(snapshot.level)
        if policy is None:
            return self._failure(
                graph_id,
                ctx,
                f"no recovery policy configured for safety level {snapshot.level.value}",
                "recovery_required",
            )
        if policy.mode == "manual" and not approved:
            return self._failure(
                graph_id,
                ctx,
                f"manual acknowledgement required to recover {snapshot.level.value}",
                "recovery_required",
            )

        ctx.error = None
        ctx.status = "running"
        object.__getattribute__(ctx, "values").update(
            {
                "recovery_for": graph_id,
                "recovery_level": snapshot.level.value,
                "recovery_mode": policy.mode,
            }
        )
        if policy.recovery_graph is not None:
            recovery_key = self._incident_graph_key(
                snapshot.incident_id, policy.recovery_graph
            )
            if recovery_key in ctx.recovery_history:
                return self._failure(
                    graph_id,
                    ctx,
                    f"recovery graph {policy.recovery_graph!r} already executed",
                    "recovery_required",
                )
            ctx.recovery_history.append(recovery_key)
            recovery_result = await self._run_graph(
                policy.recovery_graph, ctx, environment.current, body, monitor
            )
            ctx.record_output(
                f"recovery:{policy.recovery_graph}",
                {
                    "graph": policy.recovery_graph,
                    "status": recovery_result.status,
                    "success": recovery_result.success,
                },
                merge_values=False,
            )
            if not recovery_result.success:
                return self._failure(
                    graph_id,
                    ctx,
                    recovery_result.error
                    or f"recovery graph {policy.recovery_graph!r} failed",
                    recovery_result.status,
                )

        try:
            ctx.safety = await async_safety_call(
                self.safety_state,
                "recover",
                snapshot.level,
                approved=approved or policy.mode == "auto",
                include_latched=policy.clear_latched,
            )
        except (PermissionError, ValueError) as exc:
            return self._failure(graph_id, ctx, str(exc), "recovery_required")
        if ctx.safety.level.rank >= SafetyLevel.P3.rank:
            return self._failure(
                graph_id,
                ctx,
                f"safety remains at {ctx.safety.level.value}; resume is not allowed",
                "safety_stop",
            )

        if policy.resume_graph is not None:
            resume_result = await self._run_graph(
                policy.resume_graph, ctx, environment.current, body, monitor
            )
            if not resume_result.success:
                return resume_result
            ctx.record_output(
                f"resume:{policy.resume_graph}",
                {
                    "graph": policy.resume_graph,
                    "status": resume_result.status,
                    "success": resume_result.success,
                },
                merge_values=False,
            )
        ctx.status = "completed"
        await self._persist_context_async(ctx)
        return GraphResult(graph_id, "completed", True, ctx.step, ctx)

    async def _restore_context_if_available(self, ctx: GraphContext) -> None:
        """Restore a checkpoint only when this looks like a fresh context.

        An explicitly populated context always wins.  A fresh context with a
        known session id can resume after a Workflow process restart; the
        object identity supplied by the caller is preserved while its fields
        are replaced from the authoritative store.
        """
        if self.context_store is None or ctx.session_id is None:
            return
        if (
            ctx.context_revision
            or ctx.step
            or ctx.history
            or ctx.outputs
            or object.__getattribute__(ctx, "values")
            or ctx.cursors
            or ctx.graph_fingerprint
            or ctx.emergency_history
            or ctx.recovery_history
        ):
            return
        raw = await asyncio.to_thread(self.context_store.load, ctx.session_id)
        if not isinstance(raw, Mapping):
            return
        restored = GraphContext.from_dict(raw)
        self._copy_context_slots(ctx, restored)

    @staticmethod
    def _copy_context_slots(target: GraphContext, source: GraphContext) -> None:
        # ``GraphContext`` uses slots; keep this explicit so custom subclasses
        # cannot accidentally copy unrelated attributes.
        for name in GraphContext.__dataclass_fields__:
            if name in {"tools", "params", "metrics", "trace", "_running", "_values_lock", "_pending_publications"}:
                continue
            object.__setattr__(target, name, object.__getattribute__(source, name))

    async def _save_snapshot(self, snapshot):
        if self.context_store is None or snapshot.session_id is None:
            return
        # Cancellation cannot abandon a storage thread halfway through commit.
        task = asyncio.create_task(asyncio.to_thread(
            self.context_store.save, snapshot, expected_revision=snapshot.context_revision
        ))
        cancelled = False
        try:
            revision = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            revision = await task
        if revision is not None:
            snapshot.context_revision = revision
        return cancelled

    async def _persist_context_async(self, ctx: GraphContext) -> None:
        if self.context_store is None or object.__getattribute__(ctx, "session_id") is None:
            return
        async with self._checkpoint_lock:
            snapshot = GraphContext.from_dict(ctx.to_dict())
            cancelled = await self._save_snapshot(snapshot)
            ctx.context_revision = snapshot.context_revision
            if cancelled:
                raise asyncio.CancelledError

    def _activation_is_current(self, ctx, scope, node_id, generation):
        path = (*json.loads(scope), node_id, str(generation))
        for parent_scope, cursor in ctx.cursors.items():
            prefix = tuple(json.loads(parent_scope))
            if path[:len(prefix)] != prefix or len(path) < len(prefix) + 2:
                continue
            if cursor.get("terminal"):
                return False
            owner, activation = path[len(prefix):len(prefix) + 2]
            # Emergency graphs can be nested directly under a graph scope,
            # without a parent node activation in their path.
            if not activation.isdecimal():
                continue
            batch = cursor.get("batches", {}).get(activation)
            if batch is None or owner not in batch["nodes"] or owner in batch["superseded"]:
                return False
        return True

    async def _supersede_node(self, ctx, scope, node_id, generation):
        # Serializes invalidation with publication, including a durable save
        # already running in a storage thread. Never roll back committed facts.
        async with self._checkpoint_lock:
            batch = ctx.cursors[scope]["batches"].get(str(generation))
            if batch is None or node_id in batch["superseded"]:
                return
            batch["superseded"].append(node_id)
            if self.context_store is not None and object.__getattribute__(ctx, "session_id") is not None:
                snapshot = GraphContext.from_dict(ctx.to_dict())
                cancelled = await self._save_snapshot(snapshot)
                ctx.context_revision = snapshot.context_revision
                if cancelled:
                    raise asyncio.CancelledError

    async def _commit_node(self, ctx, scope, node_id, generation):
        path = json.dumps((*json.loads(scope), node_id, str(generation)))
        async with self._checkpoint_lock:
            publication = ctx._pending_publications.pop(path, None)
            if not self._activation_is_current(ctx, scope, node_id, generation):
                return
            if publication is None:
                raise RuntimeError("node completed without a prepared publication")
            record, output, changes = publication
            graph_id = json.loads(scope)[-1]
            stops_graph = self.compiled[graph_id].definition.node_map()[node_id].operation == "graph.stop"
            changes.pop("safety", None)  # SafetyStateStore is the independent authority.
            # Shared typed business fields still have one writer per wave.
            claim_patch(node_id, {"@field:" + name: value for name, value in changes.items()})
            prefix = tuple(json.loads(path))
            removed = [key for key in ctx.cursors if tuple(json.loads(key))[:len(prefix)] == prefix]
            cancelled = False
            revision = ctx.context_revision
            session_id = changes.get("session_id", object.__getattribute__(ctx, "session_id"))
            if self.context_store is not None and session_id is not None:
                snapshot = GraphContext.from_dict(ctx.to_dict())
                snapshot.node_records.setdefault(scope, {})[node_id] = record
                for name, value in changes.items():
                    object.__setattr__(snapshot, name, deepcopy(value))
                snapshot.outputs[node_id] = deepcopy(output)
                snapshot.record_scoped_output(path, deepcopy(output))
                snapshot.cursors[scope]["done"].append(node_id)
                if stops_graph:
                    snapshot.cursors[scope]["terminal"] = True
                for key in removed:
                    del snapshot.cursors[key]
                    snapshot.node_records.pop(key, None)
                cancelled = await self._save_snapshot(snapshot)
                revision = snapshot.context_revision
            # Publish only after the durable result + completion commit succeeds.
            ctx.node_records.setdefault(scope, {})[node_id] = record
            for name, value in changes.items():
                object.__setattr__(ctx, name, deepcopy(value))
            object.__getattribute__(ctx, "outputs")[node_id] = deepcopy(output)
            ctx.record_scoped_output(path, deepcopy(output))
            ctx.cursors[scope]["done"].append(node_id)
            if stops_graph:
                ctx.cursors[scope]["terminal"] = True
            for key in removed:
                ctx.cursors.pop(key, None)
                ctx.node_records.pop(key, None)
            ctx.context_revision = revision
            if cancelled:
                raise asyncio.CancelledError

        frame = current_frame(ctx)
        event_frame = ExecutionFrame(id(ctx), frame.graphs, tuple(record["path"]), node_id, record["step"], generation)
        with execution_frame(event_frame), result_view(ctx, scope, freeze(ctx.node_records.get(scope, {})), {}):
            await self._emit_event(ctx, {
                "graph": json.loads(scope)[-1], "node": node_id, "step": record["step"],
                "path": record["path"], "event": "exit", "status": None, "success": None,
            })

    async def _run_graph(self, graph_id, ctx, env, body, monitor) -> GraphResult:
        parent = current_frame(ctx)
        ancestors = parent.graphs if parent else ()
        if len(ancestors) >= self.max_depth:
            return self._failure(
                graph_id, ctx, "maximum nested graph depth exceeded", "failed"
            )
        if graph_id in ancestors:
            return self._failure(
                graph_id, ctx, "recursive graph invocation detected", "failed"
            )
        path = (*parent.path, graph_id) if parent else (graph_id,)
        frame = ExecutionFrame(id(ctx), (*ancestors, graph_id), path)
        scope = json.dumps(path)
        with execution_frame(frame), result_view(ctx, scope, freeze(ctx.node_records.get(scope, {})), {}):
            scheduler = WaveScheduler(
                self.compiled[graph_id],
                ctx,
                scope,
                lambda node, activation: self._execute_node(
                    self.graphs[graph_id],
                    node,
                    ctx,
                    env,
                    body,
                    monitor,
                    activation=activation,
                ),
                lambda: self._persist_context_async(ctx),
                lambda error, status: self._failure(graph_id, ctx, error, status),
                lambda: self._success(graph_id, ctx),
                lambda node_id, generation: self._commit_node(ctx, scope, node_id, generation),
                lambda node_id, generation: self._supersede_node(ctx, scope, node_id, generation),
            )
            result = await scheduler.run()
            if isinstance(result, GraphTransitionRequest):
                return result
            result.outputs = graph_outputs(self.graphs[graph_id], ctx, scope=scope,
                                           partial=result.status != "completed")
            return result

    async def _execute_node(
        self,
        graph: GraphDefinition,
        node: GraphNode,
        ctx: GraphContext,
        env: EnvironmentSession | EnvironmentService | None,
        body: BodyService | None,
        monitor: MonitorService | None,
        *,
        activation: int = 0,
    ) -> GraphResult | None:
        emergency = await self._check_emergency(graph, ctx, env, body, monitor)
        if emergency is not None:
            return emergency
        parent = current_frame(ctx)
        ctx.step += 1
        frame = ExecutionFrame(
            id(ctx),
            parent.graphs,
            (*parent.path, node.id, str(activation)),
            node.id,
            ctx.step,
            activation,
        )
        with execution_frame(frame):
            ctx.current_graph = graph.id
            ctx.current_node = node.id
            event = {
                "graph": graph.id,
                "node": node.id,
                "step": frame.step,
                "path": list(frame.path),
                "event": "enter",
            }
            await self._emit_event(ctx, event)
            try:
                view = current_view(ctx)
                workspace = Workspace(ctx, frame, view[2], view[3])
                if node.inputs is not None:
                    workspace.inputs = freeze(resolve_bindings(node.inputs, records=view[2],
                        inputs=object.__getattribute__(ctx, "values"), previous=workspace.previous))
                with bind_workspace(workspace):
                    result = await self._execute_node_body(graph, node, ctx, env, body, monitor)
                    if result is None:
                        ctx._pending_publications[json.dumps(frame.path)] = workspace.prepare()
            except Exception as exc:
                await self._emit_event(
                    ctx,
                    {**event, "event": "error", "error": str(exc)},
                )
                raise
            if result is not None:
                await self._emit_event(
                    ctx,
                    {
                        **event,
                        "event": "exit",
                        "status": getattr(result, "status", None),
                        "success": getattr(result, "success", None),
                    },
                    result,
                )
            return result

    async def _execute_node_body(self, graph, node, ctx, env, body, monitor):
        if self.trace is None:
            return await self._execute_node_body_impl(graph, node, ctx, env, body, monitor)

        if getattr(self.trace, "mode", "full") == "summary":
            frame = current_frame(ctx)
            with self.trace.summary_span(f"node:{graph.id}:{node.id}", attributes={
                "kind": "node", "graph": graph.id, "node": node.id,
                "step_index": frame.step if frame is not None else ctx.step,
            }) as span:
                result = await self._execute_node_body_impl(graph, node, ctx, env, body, monitor)
                if getattr(result, "status", None) in {"error", "failed"}:
                    span.status = "error"
                    span.attributes["status"] = str(result.status)
                return result
        frame = current_frame(ctx)
        cache = None
        try:
            from ..infra.observation_runtime import get_observation_cache

            cache = get_observation_cache()
        except Exception:
            cache = None
        attributes = {
            "kind": "node",
            "graph": graph.id,
            "node": node.id,
            "node_type": node.resolved_type(),
            "trigger": ctx.trigger,
            "read_versions": dict(ctx.result_versions),
            "input": trace_value(ctx.inputs),
            "operation": node.operation if node.resolved_type() == "system" else None,
            "module": node.module if node.resolved_type() == "module" else None,
            # ``step_index`` identifies this invocation globally within the
            # run; ``iteration``/``activation`` identifies the scheduler wave
            # that re-entered a loop target.
            "step_index": frame.step if frame is not None else ctx.step,
            "iteration": frame.activation if frame is not None else None,
            "activation": frame.activation if frame is not None else None,
            "step": frame.step if frame is not None else ctx.step,
            "path": list(frame.path) if frame is not None else [],
            "execution_path": list(frame.path) if frame is not None else [],
        }
        attributes.update(trace_observation(ctx.feedback, cache=cache))
        try:
            with self.trace.span(f"node:{graph.id}:{node.id}", attributes=attributes) as span:
                result = await self._execute_node_body_impl(
                    graph, node, ctx, env, body, monitor
                )
                span.attributes.update(
                    {
                        "node_span_id": str(span.span_id),
                        "output": trace_value(current_workspace(ctx).output if current_workspace(ctx) is not None else ctx.outputs.get(node.id)),
                        "status": getattr(result, "status", "ok"),
                        "success": getattr(result, "success", None),
                    }
                )
                span.attributes.update(trace_observation(ctx.feedback, cache=cache))
                return result
        except Exception:
            raise

    async def _execute_node_body_impl(self, graph, node, ctx, env, body, monitor):
        frame = current_frame(ctx)
        object.__setattr__(ctx, "current_graph", graph.id)
        object.__setattr__(ctx, "current_node", node.id)
        ctx.append_history(
            {
                "graph": graph.id,
                "node": node.id,
                "step": frame.step,
                "path": list(frame.path),
                "event": "enter",
            }
        )
        if node.resolved_type() == "graph":
            child = await self._run_graph(node.graph or "", ctx, env, body, monitor)
            if isinstance(child, GraphTransitionRequest):
                return child
            await self._record_node_output(
                ctx,
                node.id,
                {"graph": node.graph, "status": child.status, "success": child.success,
                 "outputs": child.outputs,
                 "routing_diagnostics": child.routing_diagnostics,
                 "routing_events": child.routing_events,
                 "routing_events_dropped": child.routing_events_dropped,
                 "loop_metrics": child.loop_metrics,
                 "results": {name: record["values"] for name, record in ctx.node_records.get(
                     json.dumps((*frame.path, node.graph)), {}).items()}},
            )
            if not child.success:
                return self._failure(
                    graph.id,
                    ctx,
                    child.error or f"nested graph {node.graph!r} failed",
                    child.status,
                )
            ctx.append_history(
                {
                    "graph": graph.id,
                    "node": node.id,
                    "step": frame.step,
                    "path": list(frame.path),
                    "event": "exit",
                }
            )
            return await self._check_emergency(graph, ctx, env, body, monitor)
        if node.resolved_type() == "system":
            value = await self._execute_system(
                node.operation or "", ctx, env, body, monitor
            )
            await self._record_node_output(ctx, node.id, value)
            ctx.append_history(
                {
                    "graph": graph.id,
                    "node": node.id,
                    "step": frame.step,
                    "path": list(frame.path),
                    "event": "exit",
                }
            )
            return await self._check_emergency(graph, ctx, env, body, monitor)
        module = self.modules.resolve(node.module or "")
        output = await self._invoke(module, ctx)
        if isinstance(output, ModuleResult):
            if not output.ok:
                message = output.error or f"module {node.module!r} returned ok=false"
                object.__setattr__(ctx, "error", message)
                return self._failure(graph.id, ctx, message, "failed")
            value = output.output
        else:
            value = output
        await self._record_node_output(ctx, node.id, value)
        ctx.append_history(
            {
                "graph": graph.id,
                "node": node.id,
                "step": frame.step,
                "path": list(frame.path),
                "event": "exit",
            }
        )
        return await self._check_emergency(graph, ctx, env, body, monitor)

    async def _record_node_output(self, ctx, node_id, value):
        explicit = isinstance(value, NodeResult)
        if explicit:
            ctx.values.update(value.state_patch)
            value = value.output
        ctx.record_output(node_id, value, merge_values=not explicit)
        decision = (
            value
            if isinstance(value, EvaluationDecision)
            else value.get("decision")
            if isinstance(value, Mapping)
            else None
        )
        if isinstance(decision, EvaluationDecision):
            if ctx.session_id is not None and decision.session_id != ctx.session_id:
                raise ValueError("evaluator decision belongs to another session")
            await async_reconcile_safety_snapshot(
                self.safety_state,
                "evaluator",
                decision.safety_level,
                decision.safety_signals,
                source="evaluator",
                reason=decision.reason,
                synthetic_code="evaluator.level",
            )

    async def _check_emergency(
        self,
        graph: GraphDefinition,
        ctx: GraphContext,
        env: EnvironmentSession | EnvironmentService | None,
        body: BodyService | None,
        monitor: MonitorService | None,
    ) -> GraphResult | None:
        snapshot = await async_safety_call(self.safety_state, "snapshot")
        key = self._incident_graph_key(snapshot.incident_id, graph.id)
        if key in ctx.emergency_history or key in ctx.recovery_history:
            return None
        lock = self._emergency_locks.setdefault(id(ctx), asyncio.Lock())
        async with lock:
            return await self._dispatch_emergency(graph, ctx, env, body, monitor)

    async def _dispatch_emergency(
        self,
        graph: GraphDefinition,
        ctx: GraphContext,
        env: EnvironmentSession | EnvironmentService | None,
        body: BodyService | None,
        monitor: MonitorService | None,
    ) -> GraphResult | None:
        """Poll global safety and dispatch one configured emergency graph."""
        snapshot = await async_safety_call(self.safety_state, "snapshot")
        object.__setattr__(ctx, "safety", snapshot)
        # The graph id is placed in ``emergency_history`` immediately before
        # dispatch.  While that graph is executing it must be allowed to reach
        # its body.interrupt/safe-action nodes even though the global level is
        # still P3/P4; otherwise the fail-safe guard would stop the emergency
        # graph before it could perform the safety action.
        if (
            graph.id in ctx.emergency_history
            or self._incident_graph_key(snapshot.incident_id, graph.id)
            in ctx.emergency_history
        ):
            return None
        if (
            graph.id in ctx.recovery_history
            or self._incident_graph_key(snapshot.incident_id, graph.id)
            in ctx.recovery_history
        ):
            return None
        target = graph.emergency_graph_for(snapshot.level)
        if target == graph.id:
            # A self-referential emergency mapping is only allowed while the
            # emergency graph is already running.  Otherwise it would recurse
            # forever without ever reaching the configured safe-action node.
            if (
                graph.id in ctx.emergency_history
                or self._incident_graph_key(snapshot.incident_id, graph.id)
                in ctx.emergency_history
            ):
                return None
            return self._failure(
                graph.id,
                ctx,
                f"emergency graph for {snapshot.level.value} points to itself",
                "safety_stop",
            )
        emergency_key = self._incident_graph_key(snapshot.incident_id, target)
        if emergency_key in ctx.emergency_history:
            # A parent graph must not resume normal actions after an emergency
            # graph has already been dispatched for this run.
            return self._failure(
                graph.id,
                ctx,
                f"emergency graph {target!r} already dispatched",
                "safety_stop",
            )
        if target is None:
            if (
                self.stop_on_unhandled_safety
                and snapshot.level.rank >= SafetyLevel.P3.rank
            ):
                return self._failure(
                    graph.id,
                    ctx,
                    f"unhandled safety level {snapshot.level.value}",
                    "safety_stop",
                )
            return None
        object.__getattribute__(ctx, "emergency_history").append(emergency_key)
        emergency = await self._run_graph(target, ctx, env, body, monitor)
        ctx.record_output(
            f"emergency:{target}",
            {"graph": target, "status": emergency.status, "success": emergency.success},
            merge_values=False,
        )
        reason = (
            f"safety level {snapshot.level.value}; emergency graph {target!r} executed"
        )
        if not emergency.success:
            reason += f" and failed: {emergency.error or emergency.status}"
        return self._failure(graph.id, ctx, reason, "safety_stop")

    @staticmethod
    def _incident_key(incident_id: Any) -> str | None:
        return str(incident_id) if incident_id is not None else None

    @classmethod
    def _incident_graph_key(cls, incident_id: Any, graph_id: str | None) -> str:
        if graph_id is None:
            return "<none>"
        incident = cls._incident_key(incident_id) or "legacy"
        return f"{incident}:{graph_id}"

    async def _execute_system(self, operation, ctx, env, body, monitor):
        return await self.operations.execute(operation, ctx, env, body, monitor)

    async def _invoke(self, module: Any, ctx: GraphContext) -> Any:
        object.__setattr__(ctx, "tools", self.tools)
        effective_params = ctx.params if ctx.params is not None else self.params
        object.__setattr__(ctx, "params", effective_params)
        run = getattr(module, "run", None)
        if not callable(run):
            raise TypeError(
                "graph module must implement run(ctx); use ModuleRuntime.call() for invoke() modules"
            )
        value = invoke_graph_module(run, ctx)
        if inspect.isawaitable(value):
            value = await value
        return value

    @staticmethod
    def _success(graph_id: str, ctx: GraphContext) -> GraphResult:
        return GraphResult(graph_id, "completed", True, ctx.step, ctx)

    @staticmethod
    def _failure(
        graph_id: str, ctx: GraphContext, error: str, status: GraphStatus
    ) -> GraphResult:
        object.__setattr__(ctx, "error", error)
        object.__setattr__(ctx, "status", status)
        ctx.append_history(
            {"graph": graph_id, "step": ctx.step, "event": "error", "error": error}
        )
        return GraphResult(graph_id, status, False, ctx.step, ctx, error)


__all__ = [
    "GraphContext",
    "GraphResult",
    "GraphRuntime",
    "evaluate_condition",
]
