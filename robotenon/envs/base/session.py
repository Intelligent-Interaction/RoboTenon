"""Reusable lifecycle primitives for EnvironmentSession implementations.

The protocol in :mod:`robotenon.envs.base.protocol` stays intentionally small so
third-party integrations can implement it without inheriting from RoboTenon.
Official adapters can inherit :class:`BaseEnvironmentSession` to share the
session lifecycle, command handling, sequence numbers and feedback defaults.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from ...schemas import (
    Action,
    ActionStatus,
    EnvironmentCommand,
    EnvironmentFeedback,
    EnvironmentHealth,
    EnvironmentSignal,
    Observation,
    ObservationFrame,
    SessionSpec,
)


class BaseEnvironmentSession(ABC):
    """Common state and control handling for one environment episode.

    Subclasses implement ``reset``/``observe`` and may override
    ``_new_feedback`` when they need vendor-specific frame conversion.  The
    base class owns lifecycle flags only; it never executes robot actions.
    """

    def __init__(self, spec: SessionSpec, *, session_id: UUID | None = None) -> None:
        self.spec = spec
        self.session_id: UUID = session_id or spec.episode_id or spec.run_id or uuid4()
        self.sequence_no = -1
        self.closed = False
        self.paused = False
        self.stopped = False
        self._latest_feedback: EnvironmentFeedback | None = None

    @property
    def latest_feedback(self) -> EnvironmentFeedback | None:
        return self._latest_feedback

    async def start(self) -> None:
        """Optional session hook used by service decorators."""

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError(f"environment session {self.session_id} is closed")

    def _reset_lifecycle(self, context: Any) -> None:
        """Reset per-episode bookkeeping without reconnecting the session."""

        self._ensure_open()
        if isinstance(context, SessionSpec):
            self.spec = context
        self.sequence_no = -1
        self.paused = False
        self.stopped = False
        self._latest_feedback = None

    def _next_sequence(self) -> int:
        self.sequence_no += 1
        return max(self.sequence_no, 0)

    def _publish_feedback(self, feedback: EnvironmentFeedback) -> EnvironmentFeedback:
        from ...schemas import EnvironmentIO, CameraSpec
        declared_io = self.spec.parameters.get("environment_io")
        if declared_io is not None:
            feedback.io = EnvironmentIO.model_validate(declared_io)
        if not feedback.io.cameras:
            feedback.io.cameras = {frame.frame_id: CameraSpec(
                role=frame.metadata.get("view_role", "camera"),
                flip_vertical=bool(frame.metadata.get("flip_vertical", False)))
                for frame in feedback.observation.frames if frame.modality == "rgb"}
        self.sequence_no = max(self.sequence_no, int(feedback.sequence_no))
        self._latest_feedback = feedback
        return feedback

    def _health_ready(self) -> bool:
        return not self.closed

    def _health_degraded(self) -> bool:
        return self.paused or self.stopped

    def _health_message(self) -> str | None:
        if self.closed:
            return "closed"
        if self.stopped:
            return "stopped"
        if self.paused:
            return "paused"
        return None

    def _health_model(self) -> EnvironmentHealth:
        return EnvironmentHealth(
            ready=self._health_ready(),
            degraded=self._health_degraded(),
            message=self._health_message(),
        )

    def _new_feedback(
        self,
        *,
        action: Action | None = None,
        status: ActionStatus = ActionStatus.UNKNOWN,
        observation: Observation | None = None,
        frames: Sequence[ObservationFrame] | None = None,
        state: Mapping[str, Any] | None = None,
        signals: Sequence[EnvironmentSignal] | None = None,
        reward: float | None = None,
        info: Mapping[str, Any] | None = None,
        terminal: bool = False,
        success: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> EnvironmentFeedback:
        """Build and publish a minimal canonical feedback record."""

        state_value = dict(state or {})
        if observation is None:
            observation = Observation(
                frames=list(frames or ()),
                state=state_value,
            )
        elif frames is not None:
            model_copy = getattr(observation, "model_copy", None)
            if callable(model_copy):
                observation = model_copy(update={"frames": list(frames)})
            else:
                observation = observation.copy(update={"frames": list(frames)})
        if not state_value:
            state_value = dict(getattr(observation, "state", {}) or {})
        feedback = EnvironmentFeedback(
            session_id=self.session_id,
            sequence_no=self._next_sequence(),
            timestamp=datetime.now(timezone.utc),
            observation=observation,
            action_id=action.action_id if action is not None else None,
            action_status=status,
            signals=list(signals or ()),
            state=state_value,
            reward=reward,
            info=dict(info or {}),
            health=self._health_model(),
            terminal=terminal,
            success=success,
            metadata=dict(metadata or {}),
        )
        return self._publish_feedback(feedback)

    @abstractmethod
    async def reset(self, context: Any) -> EnvironmentFeedback:
        raise NotImplementedError

    @abstractmethod
    async def observe(self, request: Any = None) -> EnvironmentFeedback:
        raise NotImplementedError

    async def health(self) -> EnvironmentHealth:
        return self._health_model()

    async def command(self, command: EnvironmentCommand) -> EnvironmentFeedback:
        """Handle lifecycle commands shared by all official sessions."""

        command_type = str(command.type)
        if command_type == "reset":
            return await self.reset(self.spec)
        self._ensure_open()
        if command_type == "pause":
            self.paused = True
            return self._new_feedback(
                info={"paused": True, "reason": command.payload.get("reason")}
            )
        if command_type == "resume":
            self.paused = False
            return self._new_feedback(
                info={"resumed": True, "reason": command.payload.get("reason")}
            )
        if command_type == "stop":
            self.stopped = True
            return self._new_feedback(
                terminal=True,
                success=False,
                info={"stopped": True, "reason": command.payload.get("reason")},
            )
        if command_type == "capture_frame":
            return await self.observe()
        return await self._custom_command(command)

    async def _custom_command(self, command: EnvironmentCommand) -> EnvironmentFeedback:
        """Default custom/reconciliation behavior: return the latest observe."""

        return await self.observe(command.payload)

    async def stop(self, reason: str = "stop_requested") -> EnvironmentFeedback:
        """Convenience stop hook for Body safety callbacks."""

        return await self.command(
            EnvironmentCommand(type="stop", payload={"reason": reason})
        )

    async def close(self, reason: str | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        await self._close_impl(reason)

    async def _close_impl(self, reason: str | None = None) -> None:
        return None


__all__ = ["BaseEnvironmentSession"]
