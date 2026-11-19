"""Small request snapshot; services do not receive scheduler internals."""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import Field

from ..schemas import EnvironmentFeedback, RoboModel, SafetyStateSnapshot, TaskItem


class ServiceContext(RoboModel):
    session_id: UUID | None = None
    task_id: str | None = None
    task_item: TaskItem | None = None
    feedback: EnvironmentFeedback | None = None
    safety: SafetyStateSnapshot = Field(default_factory=SafetyStateSnapshot)
    values: dict[str, Any] = Field(default_factory=dict)
    context_revision: int = 0

    @classmethod
    def from_context(cls, context):
        names = (
            "session_id",
            "task_id",
            "task_item",
            "feedback",
            "safety",
            "values",
            "context_revision",
        )
        if isinstance(context, Mapping):
            values = {name: context[name] for name in names if name in context}
        else:
            values = {
                name: getattr(context, name) for name in names if name != "values" and hasattr(context, name)
            }
        from .context import GraphContext
        from .workspace import current_workspace, thaw
        if not isinstance(context, (GraphContext, Mapping)) and hasattr(context, "values"):
            values["values"] = context.values
        if isinstance(context, GraphContext):
            values["values"] = context.snapshot_values() if current_workspace(context) is not None else thaw(context.inputs)
        return cls.model_validate(values)
