from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from ...schemas import (
    Action,
    ActionStatus,
    EnvironmentDescriptor,
    EnvironmentFeedback,
    EnvironmentSignal,
    Observation,
    SessionSpec,
)
from .adapter import BaseEnvironmentAdapter
from .session import BaseEnvironmentSession


class FakeEnvironmentSession(BaseEnvironmentSession):
    """Deterministic development adapter; not a simulator integration."""

    def __init__(
        self,
        spec: SessionSpec,
        *,
        max_steps: int = 3,
        success_after: int | None = None,
    ):
        super().__init__(spec)
        self.max_steps = max_steps
        self.success_after = success_after if success_after is not None else max_steps
        self.actions: list[Action] = []
        self._state: dict[str, Any] = {"step": 0, "task": spec.task_id}

    def _new_feedback(
        self,
        *,
        action: Action | None = None,
        status: ActionStatus = ActionStatus.UNKNOWN,
        terminal: bool = False,
        success: bool | None = None,
        info: Mapping[str, Any] | None = None,
    ) -> EnvironmentFeedback:
        step = self._state["step"]
        signals = [
            EnvironmentSignal(
                name="progress",
                value=step / max(self.max_steps, 1),
                source="fake",
            )
        ]
        if terminal:
            signals.append(
                EnvironmentSignal(
                    name="task_done",
                    value=bool(success),
                    source="fake",
                )
            )
        feedback = EnvironmentFeedback(
            session_id=self.session_id,
            sequence_no=self._next_sequence(),
            timestamp=datetime.now(timezone.utc),
            observation=Observation(state=dict(self._state)),
            action_id=action.action_id if action else None,
            action_status=status,
            signals=signals,
            state=dict(self._state),
            info=dict(info or {}),
            health=self._health_model(),
            terminal=terminal,
            success=success,
        )
        return self._publish_feedback(feedback)

    async def reset(self, context: Any) -> EnvironmentFeedback:
        # GraphRuntime supplies GraphContext.  The fake adapter only needs
        # the task id and keeps its original session specification.
        self._reset_lifecycle(context)
        self.actions.clear()
        self._state = {
            "step": 0,
            "task": getattr(context, "task_id", self.spec.task_id),
        }
        return self._new_feedback()

    async def observe(self, request: Any = None) -> EnvironmentFeedback:
        """Read the newest state without advancing the fake environment."""
        self._ensure_open()
        if self._latest_feedback is None:
            return self._new_feedback()
        return self._latest_feedback

class FakeEnvironmentAdapter(BaseEnvironmentAdapter):
    descriptor = EnvironmentDescriptor(
        env_id="fake",
        env_version="0.1.0",
        display_name="Fake development environment",
        capabilities={"reset", "observe", "deterministic"},
        metadata={"integration": "none"},
    )

    def __init__(self, *, max_steps: int = 3, success_after: int | None = None):
        super().__init__()
        self.max_steps = max_steps
        self.success_after = success_after

    def _create_session(self, spec: SessionSpec) -> FakeEnvironmentSession:
        return FakeEnvironmentSession(
            spec,
            max_steps=self.max_steps,
            success_after=self.success_after,
        )


# Friendly alias for examples; this is still an adapter, not a simulator.
FakeEnvironment = FakeEnvironmentAdapter


def assert_feedback_contract(feedback: EnvironmentFeedback) -> None:
    """Runtime conformance check useful for third-party adapters."""

    if feedback.sequence_no < 0:
        raise ValueError("sequence_no must be non-negative")
    if feedback.terminal and feedback.success is None and feedback.error is None:
        raise ValueError("terminal feedback must declare success or error")
