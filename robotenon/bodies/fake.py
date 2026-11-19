"""Body-side action driver for the deterministic fake environment."""

from __future__ import annotations

from typing import Any

from ..schemas import Action, ActionStatus, EnvironmentFeedback


class FakeEnvironmentActionDriver:
    """Execute fake actions without adding action methods to the Environment.

    This is deliberately a driver rather than a registered production Body.
    It provides the small deterministic action implementation needed by the
    smoke CLI and unit tests while preserving the same separation as the real
    RoboCasa and robot Body providers.
    """

    def __init__(self, session: Any) -> None:
        self.session = session

    async def execute(self, action: Action) -> EnvironmentFeedback:
        session = self.session
        session._ensure_open()
        if session.paused or session.stopped:
            raise RuntimeError("fake environment session is paused or stopped")
        if action.session_id != session.session_id:
            raise ValueError("action belongs to a different session")
        session.actions.append(action)
        session._state["step"] += 1
        terminal = session._state["step"] >= session.max_steps
        success = terminal and session._state["step"] >= session.success_after
        return session._new_feedback(
            action=action,
            status=ActionStatus.SUCCEEDED,
            terminal=terminal,
            success=success if terminal else None,
        )


__all__ = ["FakeEnvironmentActionDriver"]
