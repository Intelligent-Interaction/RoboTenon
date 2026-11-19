"""Explicit graph-node output; generic invoke() retains ModuleResult."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schemas import EnvironmentFeedback, TaskItem
from .context import GraphContext


@dataclass(slots=True)
class ModuleContext:
    """Context supplied to a module call; domain adapters may add values.

    ``params`` is the immutable run configuration.  Environment is purposely
    absent: use the process-resident ``robotenon.envs.environment`` handle.
    """

    feedback: EnvironmentFeedback | None = None
    task: TaskItem | None = None
    values: dict[str, Any] = field(default_factory=dict)
    # Live resident ToolRuntime injected by bootstrap.  It is intentionally
    # not part of the serializable GraphContext snapshot.
    tools: Any = None
    # Immutable experiment parameter view, injected by bootstrap.
    params: Any = None
    # Optional live observability handles; omitted from serialized context.
    metrics: Any = None
    trace: Any = None

    @property
    def env(self) -> Any:
        """Compatibility access to the process-resident environment handle."""

        from ..envs.runtime import environment

        return environment

    @env.setter
    def env(self, value: Any) -> None:
        if value is None:
            return
        from ..envs.runtime import bind_environment, environment

        if value is not environment:
            bind_environment(value)

    @property
    def session_id(self) -> Any:
        """Expose the current session to ToolContext-compatible providers."""

        if self.feedback is not None:
            return self.feedback.session_id
        return getattr(self.task, "session_id", None)

    @property
    def task_item(self) -> TaskItem | None:
        """Alias used by tools that also receive a GraphContext."""

        return self.task

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ):
        """Call a resident tool without constructing a provider locally."""

        if self.tools is None:
            raise RuntimeError("module has no ToolRuntime; initialize tools and inject them")
        return await self.tools.call(name, arguments, context=self)


@dataclass(slots=True)
class ModuleResult:
    """Stable result envelope shared by every module category."""

    ok: bool
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NodeResult:
    """Output plus a patch to the current node's private values, never global state."""
    output: Any = None
    state_patch: dict[str, Any] = field(default_factory=dict)


class GraphModule(Protocol):
    """Canonical graph-node contract.

    GraphRuntime invokes this entry point with the one shared
    :class:`GraphContext`; no environment, tool or parameter positional
    arguments are injected.
    """

    async def run(self, ctx: GraphContext) -> NodeResult: ...


class CallableModule(Protocol):
    """Compatibility contract for direct string/module calls.

    ``invoke`` is intentionally separate from ``GraphModule.run``.  A module
    implementing only this protocol is valid for ``ModuleRuntime.call`` but
    must be adapted explicitly before it is placed in a JSON graph.
    """

    async def invoke(
        self, arguments: Mapping[str, Any], context: ModuleContext
    ) -> ModuleResult: ...


__all__ = [
    "CallableModule",
    "GraphModule",
    "ModuleContext",
    "ModuleResult",
    "NodeResult",
]
