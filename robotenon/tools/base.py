"""Resident tool contracts and lifecycle-managed tool runtime.

Tools are deliberately separate from Workflow Modules.  A Module describes a
piece of graph/business logic (policy, evaluator, planner, ...); a Tool is a
long-lived capability that a Module may call (model inference, memory, skill
lookup, or an application-specific utility). Tool instances are constructed
once from the merged master configuration (whose child manifest is commonly
``configs/tools_configs.json``) and remain resident until the runtime is
closed.
"""

from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts.execution import current_frame


@dataclass(slots=True)
class ToolResult:
    """Stable result envelope returned by :class:`ToolRuntime.call`.

    A tool implementation may return this envelope or a raw value.  Raw
    values are wrapped as ``ok=True`` by the runtime so callers can choose a
    compact implementation without giving up a consistent error boundary.
    """

    ok: bool = True
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, **_: Any) -> dict[str, Any]:
        """Pydantic-style spelling used by the framework serializers."""

        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ToolContext:
    """Runtime view passed to a tool.

    ``graph_context`` is the shared global ``GraphContext`` when the call was
    made from a graph.  It is intentionally a live reference, while the
    graph's durable snapshot omits this runtime handle.  Tools can call other
    resident tools through :meth:`call`, but should avoid creating their own
    model/database clients.
    """

    graph_context: Any = None
    runtime: ToolRuntime | None = None
    # Ephemeral decoded view injected by ToolRuntime when an observation
    # resolver is configured.  It is deliberately not copied into
    # GraphContext/checkpoints.
    observation_view: Any = None

    @property
    def values(self) -> dict[str, Any]:
        from ..contracts.context import GraphContext
        from ..contracts.workspace import current_workspace
        if isinstance(self.graph_context, GraphContext) and current_workspace(self.graph_context) is None:
            return self.graph_context.inputs
        values = getattr(self.graph_context, "values", None)
        return values if isinstance(values, Mapping) else {}

    @property
    def session_id(self) -> Any:
        return getattr(self.graph_context, "session_id", None)

    @property
    def task_item(self) -> Any:
        value = getattr(self.graph_context, "task_item", None)
        if value is not None:
            return value
        return getattr(self.graph_context, "task", None)

    @property
    def feedback(self) -> Any:
        return getattr(self.graph_context, "feedback", None)

    @property
    def environment_io(self):
        from ..schemas import EnvironmentIO
        return getattr(self.feedback, "io", None) or EnvironmentIO()

    @property
    def observation(self) -> Any:
        """Decoded observation view, when the runtime was configured."""

        return self.observation_view

    @property
    def params(self) -> Any:
        return getattr(self.graph_context, "params", None)

    @property
    def metrics(self) -> Any:
        """Return the run-scoped MetricsClient, when metrics are enabled."""

        value = getattr(self.graph_context, "metrics", None)
        return value if value is not None else getattr(self.runtime, "metrics", None)

    @property
    def trace(self) -> Any:
        """Return the run-scoped TraceClient, when tracing is enabled."""

        value = getattr(self.graph_context, "trace", None)
        return value if value is not None else getattr(self.runtime, "trace", None)

    @property
    def env(self) -> Any:
        # Environment is process-resident rather than a per-tool input.  Keep
        # this convenience property for providers that prefer ``context.env``
        # while returning the singleton proxy, never a copied session/client.
        from ..envs.runtime import environment

        return environment

    @property
    def ctx(self) -> Any:
        """Short alias for the live GraphContext/ModuleContext."""

        return self.graph_context

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        """Call another tool while preserving the same graph context."""

        if self.runtime is None:
            raise RuntimeError("tool context is not attached to a ToolRuntime")
        return await self.runtime.call(
            name,
            arguments,
            context=self.graph_context,
        )

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        """Alias matching ``GraphContext.call_tool``."""

        return await self.call(name, arguments)


class Tool(Protocol):
    """Recommended implementation contract for a resident tool."""

    async def call(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult | Any: ...


class BaseTool(ABC):
    """Official base class for resident capabilities.

    A Tool owns a reusable capability such as model inference, memory or a
    skill registry.  It must implement ``call(arguments, context)``; startup
    and shutdown hooks are optional and are made concrete here so providers
    only need to override them when they own resources.
    """

    async def start(self) -> None:
        """Optional one-time resource acquisition hook."""

    @abstractmethod
    async def call(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult | Any:
        """Execute one capability request."""

    async def close(self) -> None:
        """Optional shutdown hook."""


def _invoke_callable(
    method: Any,
    arguments: Mapping[str, Any],
    tool_context: ToolContext,
    *,
    graph_context: Any,
    run_method: bool = False,
) -> Any:
    """Invoke a plugin method across the small set of supported signatures.

    The preferred signature is ``call(arguments, context)`` (or the keyword
    spelling ``context=``).  ``invoke`` is accepted for migration from older
    extensions, and ``run(ctx)`` is useful for tiny graph-aware utilities.
    Signature inspection avoids catching an implementation's internal
    ``TypeError`` and accidentally invoking it twice.
    """

    try:
        parameters = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        parameters = []
    positional = [
        item
        for item in parameters
        if item.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
    names = {item.name for item in parameters}
    arguments_parameter = next(
        (item for item in parameters if item.name in {"arguments", "args", "payload"}),
        None,
    )

    if run_method:
        value = graph_context if graph_context is not None else tool_context
        return method(value)
    if arguments_parameter is not None and arguments_parameter.kind == inspect.Parameter.KEYWORD_ONLY:
        if "context" in names or accepts_kwargs:
            return method(**{arguments_parameter.name: arguments, "context": tool_context})
        if "ctx" in names:
            return method(**{arguments_parameter.name: arguments, "ctx": tool_context})
        return method(**{arguments_parameter.name: arguments})
    if "context" in names or accepts_kwargs:
        return method(arguments, context=tool_context)
    if "ctx" in names:
        return method(arguments, ctx=tool_context)
    if len(positional) >= 2:
        return method(arguments, tool_context)
    if len(positional) == 0:
        return method()
    return method(arguments)


class ToolRuntime(Mapping[str, Tool]):
    """Resident, registry-addressable tool instances.

    ``ToolRuntime`` is intentionally analogous to ``ModuleRuntime`` but has a
    different namespace and call contract.  ``start`` and ``close`` are
    idempotent, and startup rolls back already-started tools if a later tool
    fails.  This makes it safe to keep the runtime in a process supervisor.
    """

    def __init__(
        self,
        instances: Mapping[str, Tool],
        *,
        aliases: Mapping[str, str] | None = None,
        metadata: Mapping[str, Mapping[str, Any]] | None = None,
        observation_resolver: Any = None,
    ) -> None:
        self._instances = dict(instances)
        self._aliases = dict(aliases or {})
        self._metadata = {key: dict(value) for key, value in (metadata or {}).items()}
        if observation_resolver is None:
            # A directly constructed ToolRuntime should still participate in
            # the process-resident observation infrastructure.  The import is
            # lazy to avoid a tools -> infra -> schemas import cycle during
            # package discovery.
            try:
                from ..infra.observation_runtime import get_observation_resolver

                observation_resolver = get_observation_resolver()
            except Exception:
                observation_resolver = None
        self.observation_resolver = observation_resolver
        self._order = tuple(self._instances)
        self._started = False
        self._lifecycle_lock = asyncio.Lock()
        self.trace: Any = None
        self.metrics: Any = None

    def __getitem__(self, key: str) -> Tool:
        return self.resolve(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._instances)

    def resolve(self, name: str) -> Tool:
        key = name if name in self._instances else self._aliases.get(name)
        if key is None or key not in self._instances:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown tool {name!r}; available: {available}")
        return self._instances[key]

    def names(self) -> list[str]:
        """Return configured instance ids; aliases remain accepted by resolve."""

        return list(self._order)

    @property
    def started(self) -> bool:
        return self._started

    def set_observation_resolver(self, resolver: Any) -> None:
        """Attach/replace the resident URI resolver without rebuilding tools."""

        self.observation_resolver = resolver

    def set_trace(self, trace: Any = None) -> None:
        """Attach the optional durable trace sink used by graph executions."""

        self.trace = trace

    def set_metrics(self, metrics: Any = None) -> None:
        """Attach the optional MetricsClient used by detached tool calls."""

        self.metrics = metrics

    async def end_session(self, session_id: str) -> None:
        """Release task state without shutting down resident capabilities."""
        first_error = None
        try:
            for tool in self._instances.values():
                hook = getattr(tool, "end_session", None)
                if hook is not None:
                    try:
                        result = hook(session_id)
                        if inspect.isawaitable(result):
                            await result
                    except Exception as exc:
                        first_error = first_error or exc
        finally:
            self.set_trace(None)
            self.set_metrics(None)
            self.set_observation_resolver(None)
        if first_error is not None:
            raise first_error

    async def start(self) -> None:
        """Start all resident tools exactly once."""

        async with self._lifecycle_lock:
            if self._started:
                return
            started: list[Tool] = []
            try:
                for key in self._order:
                    tool = self._instances[key]
                    started.append(tool)
                    hook = getattr(tool, "start", None)
                    if hook is not None:
                        result = hook()
                        if inspect.isawaitable(result):
                            await result
                self._started = True
            except BaseException:
                for tool in reversed(started):
                    try:
                        hook = getattr(tool, "close", None)
                        if hook is not None:
                            result = hook()
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        pass
                raise

    async def close(self) -> None:
        """Close resident tools in reverse configuration order."""

        async with self._lifecycle_lock:
            if not self._started:
                return
            first_error: Exception | None = None
            for key in reversed(self._order):
                try:
                    hook = getattr(self._instances[key], "close", None)
                    if hook is not None:
                        result = hook()
                        if inspect.isawaitable(result):
                            await result
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
            self._started = False
            if first_error is not None:
                raise first_error

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        context: Any = None,
        ctx: Any = None,
    ) -> ToolResult:
        """Call a resident tool by configured id or registry string.

        ``ctx=`` is a concise alias for ``context=`` so modules can write
        ``await ctx.call_tool('model', {...})`` without copying state.
        """

        trace = self.trace
        if trace is None:
            return await self._call_impl(name, arguments, context=context, ctx=ctx)

        if getattr(trace, "mode", "full") == "summary":
            with trace.summary_span(f"tool:{name}", attributes={"kind": "tool", "tool": str(name)}) as span:
                result = await self._call_impl(name, arguments, context=context, ctx=ctx)
                if not result.ok:
                    span.status = "error"
                    span.attributes["error"] = str(result.error)[:8000]
                return result

        from ..infra.trace import trace_observation, trace_value

        key = name if name in self._instances else self._aliases.get(name, name)
        metadata = self._metadata.get(key, {})
        role = metadata.get("role", "tool")
        graph_context = context if context is not None else ctx
        observation_cache = getattr(self.observation_resolver, "cache", None)
        frame = current_frame(graph_context) if graph_context is not None else None
        attributes = {
            "kind": "tool",
            "tool": str(name),
            "tool_id": str(key),
            "tool_role": role,
            "arguments": trace_value(dict(arguments or {})),
        }
        if frame is not None:
            attributes.update(
                {
                    "caller_graph": frame.graphs[-1] if frame.graphs else None,
                    "caller_node": frame.node,
                    "caller_step_index": frame.step,
                    "caller_iteration": frame.activation,
                    "caller_activation": frame.activation,
                    "caller_path": list(frame.path),
                }
            )
        attributes.update(
            trace_observation(
                getattr(graph_context, "feedback", None),
                cache=observation_cache,
            )
        )
        try:
            with trace.span(f"tool:{name}", attributes=attributes) as span:
                result = await self._call_impl(name, arguments, context=context, ctx=ctx)
                span.attributes.update(
                    {
                        "tool_span_id": str(span.span_id),
                        "parent_span_id": (
                            str(span.parent_span_id)
                            if span.parent_span_id is not None
                            else None
                        ),
                        "ok": bool(result.ok),
                        "result": trace_value(result.output),
                        "result_metadata": trace_value(result.metadata),
                    }
                )
                if result.error:
                    span.attributes["error"] = str(result.error)
                if not result.ok:
                    span.status = "error"
                return result
        except Exception:
            raise

    async def _call_impl(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        context: Any = None,
        ctx: Any = None,
    ) -> ToolResult:
        """Execute a tool call without observability wrapping."""

        if context is not None and ctx is not None:
            raise TypeError("pass either context or ctx, not both")
        graph_context = context if context is not None else ctx
        await self.start()
        tool = self.resolve(name)
        observation_view = None
        resolver = self.observation_resolver
        if resolver is None:
            # A directly constructed runtime may be created before the
            # application bootstrap binds the resident cache.  Resolve the
            # handle lazily at call time so tool instances remain reusable.
            try:
                from ..infra.observation_runtime import get_observation_resolver

                resolver = get_observation_resolver()
            except Exception:
                resolver = None
        feedback = getattr(graph_context, "feedback", None)
        if resolver is not None and feedback is not None:
            resolve_feedback = getattr(resolver, "resolve_feedback", None)
            if callable(resolve_feedback):
                observation_view = resolve_feedback(feedback)
                if inspect.isawaitable(observation_view):
                    observation_view = await observation_view
        tool_context = ToolContext(
            graph_context=graph_context,
            runtime=self,
            observation_view=observation_view,
        )
        args = dict(arguments or {})
        if observation_view is not None:
            # Explicit caller arguments win, which permits a tool to request
            # a deliberately different frame set for an experiment.
            args.setdefault("observation_view", observation_view)
        call = getattr(tool, "call", None)
        invoke = getattr(tool, "invoke", None)
        run = getattr(tool, "run", None)
        if callable(call):
            value = _invoke_callable(call, args, tool_context, graph_context=graph_context)
        elif callable(invoke):
            value = _invoke_callable(invoke, args, tool_context, graph_context=graph_context)
        elif callable(run):
            value = _invoke_callable(
                run,
                args,
                tool_context,
                graph_context=graph_context,
                run_method=True,
            )
        else:
            raise TypeError(f"tool {name!r} does not implement call(), invoke() or run(ctx)")
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, ToolResult):
            return value
        return ToolResult(ok=True, output=value)

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        context: Any = None,
        ctx: Any = None,
    ) -> ToolResult:
        """Compatibility alias for callers that use ``invoke`` terminology."""

        return await self.call(name, arguments, context=context, ctx=ctx)

    async def __aenter__(self) -> ToolRuntime:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "id": key,
                **self._metadata.get(key, {}),
                "started": self._started,
            }
            for key in self._order
        ]


__all__ = ["BaseTool", "Tool", "ToolContext", "ToolResult", "ToolRuntime"]
