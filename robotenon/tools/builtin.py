"""Small, dependency-free resident tools used by the framework skeleton.

The built-ins are intentionally useful without downloading a model or
starting another service.  Production projects can register a provider under
the same categories (``model``, ``memory`` or ``skill``) and select it in
``tools_configs.json`` without changing Workflow code.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..extensions.memory import MemoryStore
from ..extensions.skills import SkillRegistry
from ..schemas import MemoryRecord, SkillDefinition
from .base import BaseTool, ToolContext, ToolResult
from .registry import register_tool


@register_tool(
    "model",
    version="1.0.0",
    kind="model",
    description="Dependency-free model adapter; echo/static backends are available by default.",
)
class ModelTool(BaseTool):
    """Resident model adapter with a deliberately small provider boundary.

    ``backend=echo`` is a deterministic smoke implementation.  ``backend=
    static`` returns the configured ``response``.  A real provider should be
    registered as a new tool or subclass this adapter and keep the same
    ``call(arguments, context)`` contract.
    """

    def __init__(
        self,
        *,
        backend: str = "echo",
        model: str = "mock-model",
        response: Any = None,
        prefix: str = "",
    ) -> None:
        self.backend = str(backend).lower()
        self.model = model
        self.response = response
        self.prefix = prefix
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._started = False

    async def call(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        prompt = arguments.get("prompt", arguments.get("input", arguments.get("text", "")))
        if self.backend == "echo":
            output: Any = {
                "text": f"{self.prefix}{prompt}",
                "model": self.model,
                "backend": self.backend,
            }
        elif self.backend == "static":
            output = self.response
        else:
            return ToolResult(
                ok=False,
                error=(
                    f"unsupported model backend {self.backend!r}; register a provider "
                    "or use backend='echo'/'static'"
                ),
            )
        return ToolResult(
            ok=True,
            output=output,
            metadata={"tool": "model", "session_id": str(context.session_id) if context.session_id else None},
        )


@register_tool(
    "memory",
    version="1.0.0",
    kind="memory",
    description="Resident quality-gated episode memory backed by SQLite or in-memory storage.",
)
class MemoryTool(BaseTool):
    """Expose the existing :class:`MemoryStore` through the tool boundary."""

    def __init__(self, *, path: str = ":memory:") -> None:
        self.path = path
        self.store: MemoryStore | None = None

    async def start(self) -> None:
        if self.store is None:
            self.store = MemoryStore(self.path)

    async def call(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        operation = str(arguments.get("op", arguments.get("operation", "search"))).lower()
        if operation == "add":
            raw = arguments.get("record")
            if isinstance(raw, MemoryRecord):
                record = raw
            elif isinstance(raw, Mapping):
                record = MemoryRecord.model_validate(dict(raw))
            else:
                raise ValueError("memory.add requires record=<MemoryRecord or object>")
            if self.store is None:  # ToolRuntime normally starts it first.
                await self.start()
            self.store.add(record, quality_ok=bool(arguments.get("quality_ok", True)))
            return ToolResult(ok=True, output={"memory_id": str(record.memory_id), "stored": True})
        if operation == "search":
            limit = int(arguments.get("limit", 20))
            if limit < 1:
                raise ValueError("memory.search limit must be >= 1")
            if self.store is None:  # ToolRuntime normally starts it first.
                await self.start()
            records = self.store.search(
                task_id=arguments.get("task_id"),
                outcome=arguments.get("outcome"),
                tag=arguments.get("tag"),
                limit=limit,
            )
            return ToolResult(ok=True, output={"records": records, "count": len(records)})
        raise ValueError("memory operation must be 'add' or 'search'")

    async def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None


@register_tool(
    "skill",
    version="1.0.0",
    kind="skill",
    description="Resident immutable versioned skill registry.",
)
class SkillTool(BaseTool):
    """Expose versioned skill registration and lookup to Modules."""

    def __init__(self, *, skills: list[Mapping[str, Any]] | None = None) -> None:
        self.registry = SkillRegistry()
        for raw in skills or []:
            self.registry.register(SkillDefinition.model_validate(dict(raw)))

    async def call(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        operation = str(arguments.get("op", arguments.get("operation", "heads"))).lower()
        if operation == "heads":
            return ToolResult(ok=True, output={"skills": self.registry.heads()})
        if operation == "get":
            skill_id = arguments.get("skill_id")
            version = arguments.get("version")
            if not skill_id or not version:
                raise ValueError("skill.get requires skill_id and version")
            return ToolResult(ok=True, output=self.registry.get(str(skill_id), str(version)))
        if operation == "register":
            raw = arguments.get("skill")
            if not isinstance(raw, Mapping):
                raise ValueError("skill.register requires skill=<object>")
            skill = self.registry.register(SkillDefinition.model_validate(dict(raw)))
            return ToolResult(ok=True, output=skill)
        raise ValueError("skill operation must be 'heads', 'get' or 'register'")


@register_tool(
    "echo",
    version="1.0.0",
    kind="utility",
    description="Return arguments and selected shared-context fields for smoke tests.",
)
class EchoTool(BaseTool):
    def __init__(self, *, prefix: str = "") -> None:
        self.prefix = prefix

    async def call(self, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult(
            ok=True,
            output={
                "prefix": self.prefix,
                "arguments": dict(arguments),
                "session_id": str(context.session_id) if context.session_id is not None else None,
            },
        )


__all__ = ["EchoTool", "MemoryTool", "ModelTool", "SkillTool"]
