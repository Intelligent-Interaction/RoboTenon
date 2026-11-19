from __future__ import annotations

import asyncio
import inspect
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from typing import Any, Protocol

from ..contracts.context import GraphContext
from ..contracts.module import ModuleContext, ModuleResult


class Module(Protocol):
    """Common call boundary for all resident modules.

    A graph node implements ``run(ctx)``.  The shared ``ctx`` contains the
    immutable experiment parameters at ``ctx.params``.  Environment is a
    process-resident singleton (``robotenon.envs.environment``), so modules do
    not receive a second ``env`` argument or create their own connection.
    """

    async def invoke(
        self, arguments: Mapping[str, Any], context: ModuleContext
    ) -> ModuleResult: ...


class BaseModule(ABC):
    """Official base class for graph modules.

    The graph contract is deliberately one method, ``run(ctx)``. ``ctx`` is
    the shared :class:`~robotenon.contracts.context.GraphContext`; parameters,
    observations and resident Tools are reached through that object. The
    older ``Module`` protocol remains available for third-party providers
    that only need the structural ``invoke`` compatibility API.
    """

    async def start(self) -> None:
        """Optional one-time startup hook."""

    @abstractmethod
    async def run(self, ctx: GraphContext) -> Any:
        """Execute one graph node against the shared context."""

    async def close(self) -> None:
        """Optional shutdown hook."""


def invoke_graph_module(
    module_run: Any,
    ctx: GraphContext,
):
    """Call a graph module with its single supported input: ``ctx``.

    Environment is obtained from the process-resident
    ``robotenon.envs.environment`` handle; parameters are available as
    ``ctx.params``. Keeping the call boundary strictly one-argument prevents
    an accidental second environment/client from being constructed by a
    module.
    """

    try:
        parameters = list(inspect.signature(module_run).parameters.values())
    except (TypeError, ValueError):
        return module_run(ctx)
    positional = [
        item
        for item in parameters
        if item.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    # The contract is run(ctx).  A keyword-only spelling such as
    # run(*, ctx) is accepted for convenience; no env/params keyword is ever
    # injected.  Required extra positional arguments intentionally raise the
    # normal Python TypeError and point the extension author at the contract.
    if positional:
        return module_run(ctx)
    ctx_parameter = next(
        (
            item
            for item in parameters
            if item.kind == inspect.Parameter.KEYWORD_ONLY
            and item.name.lower() in {"ctx", "context", "graph_context"}
        ),
        None,
    )
    if ctx_parameter is not None:
        return module_run(**{ctx_parameter.name: ctx})
    return module_run(ctx)


class ModuleRuntime(Mapping[str, Module]):
    """Resident module container initialized once per application.

    Instances are constructed from the merged master configuration (whose
    child manifest is commonly ``modules_configs.json``) and kept in memory
    until ``close``. Calls accept either the configured ``id`` or an
    unambiguous registry string.  Optional ``start``/``close`` hooks allow
    modules to own lightweight state; heavyweight model, memory and skill
    providers should instead be injected as the resident ``tools`` runtime.
    """

    def __init__(
        self,
        instances: Mapping[str, Module],
        *,
        aliases: Mapping[str, str] | None = None,
        metadata: Mapping[str, Mapping[str, Any]] | None = None,
        tools: Any = None,
        params: Any = None,
        metrics: Any = None,
        trace: Any = None,
    ):
        self._instances = dict(instances)
        self._aliases = dict(aliases or {})
        self._metadata = {key: dict(value) for key, value in (metadata or {}).items()}
        self._order = tuple(self._instances)
        self._started = False
        self._lifecycle_lock = asyncio.Lock()
        # A reference only; ToolRuntime owns its own lifecycle.  Bootstrap
        # starts tools before modules and closes modules before tools.
        self.tools = tools
        self.params = params
        self.metrics = metrics
        self.trace = trace
        self.bind_tools(tools)

    def bind_tools(self, tools: Any = None) -> None:
        """Expose the resident ToolRuntime on module instances when possible.

        Graph calls always receive ``ctx.tools`` as the authoritative handle.
        This optional object binding additionally lets a module use a tool in
        its one-time ``start`` hook without forcing every plugin constructor
        to accept a framework-specific argument.  A plugin may implement a
        ``set_tools`` method for explicit binding; immutable/slot-based
        plugins simply rely on the context call path.
        """

        if tools is None:
            return
        for module in self._instances.values():
            setter = getattr(module, "set_tools", None)
            if callable(setter):
                setter(tools)
                continue
            try:
                module.tools = tools
            except (AttributeError, TypeError):
                # ``ctx.tools`` remains available for slots/frozen plugins.
                pass

    def __getitem__(self, key: str) -> Module:
        return self.resolve(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._order)

    def __len__(self) -> int:
        return len(self._instances)

    def resolve(self, name: str) -> Module:
        key = name if name in self._instances else self._aliases.get(name)
        if key is None or key not in self._instances:
            available = ", ".join(self.names()) or "<none>"
            raise KeyError(f"unknown module {name!r}; available: {available}")
        return self._instances[key]

    def names(self) -> list[str]:
        """Return configured instance ids (aliases remain accepted)."""
        return list(self._order)

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        """Start every resident module once, rolling back partial startup."""
        async with self._lifecycle_lock:
            if self._started:
                return
            started: list[Module] = []
            try:
                for key in self._order:
                    module = self._instances[key]
                    started.append(module)
                    hook = getattr(module, "start", None)
                    if hook is not None:
                        result = hook()
                        if inspect.isawaitable(result):
                            await result
                self._started = True
            except BaseException:
                for module in reversed(started):
                    try:
                        hook = getattr(module, "close", None)
                        if hook is not None:
                            result = hook()
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        pass  # Keep the original startup/cancellation error.
                raise

    async def close(self) -> None:
        """Close resident modules in reverse configuration order."""
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
        *,
        context: ModuleContext | None = None,
    ) -> ModuleResult:
        """Call a resident module by configured id or registration string."""
        await self.start()
        module = self.resolve(name)
        invoke = getattr(module, "invoke", None)
        run = getattr(module, "run", None)
        if callable(invoke):
            if isinstance(context, GraphContext):
                # A graph caller may use the compatibility string-call API;
                # adapt the same shared object to the legacy invoke DTO
                # rather than silently constructing a detached context.
                effective_context = context.module_context()
            else:
                effective_context = context if context is not None else ModuleContext()
            if getattr(effective_context, "tools", None) is None:
                effective_context.tools = self.tools
            if getattr(effective_context, "params", None) is None:
                effective_context.params = self.params
            if getattr(effective_context, "metrics", None) is None:
                effective_context.metrics = self.metrics
            if getattr(effective_context, "trace", None) is None:
                effective_context.trace = self.trace
            result = invoke(arguments or {}, effective_context)
        elif callable(run):
            # Graph-only modules always receive GraphContext, including when
            # invoked through the compatibility string-call API.
            effective_params = context.params if context is not None else self.params
            if isinstance(context, GraphContext):
                # Preserve the caller's identity and all graph fields when a
                # node is called from another node/tool.  This is the same
                # shared ctx contract used by GraphRuntime.
                supplied = context
                supplied.tools = self.tools
                supplied.params = effective_params
                if supplied.metrics is None:
                    supplied.metrics = self.metrics
                if supplied.trace is None:
                    supplied.trace = self.trace
            else:
                supplied = GraphContext(
                    values=dict(context.values if context is not None else arguments or {}),
                    feedback=context.feedback if context is not None else None,
                    task_item=context.task if context is not None else None,
                    session_id=context.feedback.session_id
                    if context is not None and context.feedback
                    else None,
                    tools=self.tools,
                    params=effective_params,
                    metrics=self.metrics,
                    trace=self.trace,
                )
            result = invoke_graph_module(run, supplied)
        else:
            raise TypeError(f"module {name!r} does not implement invoke() or run(ctx)")
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ModuleResult):
            return result
        return ModuleResult(ok=True, output=result)

    async def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        context: ModuleContext | None = None,
    ) -> ModuleResult:
        """Alias for :meth:`call`."""
        return await self.call(name, arguments, context=context)

    async def __aenter__(self) -> ModuleRuntime:
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
