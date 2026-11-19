"""Reference real-robot Environment adapter.

The core package deliberately does not import ROS2 or a vendor SDK.  A
deployment supplies an observation source object (or replaces this provider
with a site-specific plugin) while the graph still talks to the same
``EnvironmentSession`` protocol as RoboCasa.

This module is safe to import on a workstation without robot dependencies.
When no source is configured it provides a state-only development session and
reports ``ready=False`` so a real run cannot accidentally look healthy.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from ..schemas import (
    ActionStatus,
    EnvironmentDescriptor,
    EnvironmentFeedback,
    EnvironmentHealth,
    EnvironmentSignal,
    Observation,
    ObservationFrame,
    SessionSpec,
)
from .base import BaseEnvironmentAdapter, BaseEnvironmentSession, register_environment


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _model_validate(model: Any, value: Any) -> Any:
    validator = getattr(model, "model_validate", None)
    if callable(validator):
        return validator(value)
    return model.parse_obj(value)


def _frame_from_value(frame_id: str, value: Any) -> ObservationFrame:
    if isinstance(value, ObservationFrame):
        return value
    if isinstance(value, Mapping):
        candidate = dict(value)
        candidate.setdefault("frame_id", frame_id)
        candidate.setdefault("role", "policy_input")
        candidate.setdefault(
            "modality",
            "rgb"
            if "image" in frame_id.lower() or "camera" in frame_id.lower()
            else "state",
        )
        candidate.setdefault("producer", "physical_sensor")
        candidate.setdefault(
            "camera_id",
            frame_id if candidate["modality"] == "rgb" else None,
        )
        return _model_validate(ObservationFrame, candidate)
    modality = (
        "rgb"
        if "image" in frame_id.lower() or "camera" in frame_id.lower()
        else "state"
    )
    shape = getattr(value, "shape", None)
    return ObservationFrame(
        frame_id=frame_id,
        role="policy_input",
        modality=modality,
        producer="physical_sensor",
        camera_id=frame_id if modality == "rgb" else None,
        shape=[int(item) for item in shape] if shape is not None else None,
        encoding="raw",
        color_space="rgb" if modality == "rgb" else None,
        data=value,
    )


def _frames_from_value(value: Any) -> list[ObservationFrame]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [_frame_from_value(str(key), item) for key, item in value.items()]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            item
            if isinstance(item, ObservationFrame)
            else _frame_from_value(f"frame_{index}", item)
            for index, item in enumerate(value)
        ]
    return [_frame_from_value("frame_0", value)]


def _signals_from_value(value: Any) -> list[EnvironmentSignal]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [
            {"name": str(key), "value": item, "source": "real_robot"}
            for key, item in value.items()
        ]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        value = [{"name": "signal", "value": value, "source": "real_robot"}]
    signals: list[EnvironmentSignal] = []
    for index, item in enumerate(value):
        if isinstance(item, EnvironmentSignal):
            signals.append(item)
        elif isinstance(item, Mapping):
            candidate = dict(item)
            candidate.setdefault("name", f"signal_{index}")
            candidate.setdefault("source", "real_robot")
            signals.append(_model_validate(EnvironmentSignal, candidate))
        else:
            signals.append(
                EnvironmentSignal(
                    name=f"signal_{index}",
                    value=item,
                    source="real_robot",
                )
            )
    return signals


class RealRobotSession(BaseEnvironmentSession):
    """One physical robot observation channel.

    ``observation_source`` is intentionally duck-typed.  It may implement
    ``reset``, ``observe`` and ``close`` as sync or async methods and may
    return an ``EnvironmentFeedback``, ``Observation`` or a small mapping.
    Action execution is not implemented here; :class:`RealRobotBody` owns it.
    """

    def __init__(
        self,
        spec: SessionSpec,
        *,
        robot_name: str = "robot0",
        observation_source: Any = None,
        initial_state: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(spec)
        self.robot_name = robot_name
        self.observation_source = observation_source
        self.initial_state = dict(initial_state or {})

    def _health_ready(self) -> bool:
        return super()._health_ready() and self.observation_source is not None

    def _health_message(self) -> str | None:
        message = super()._health_message()
        if message is not None:
            return message
        if self.observation_source is None:
            return "observation_source_not_configured"
        return None

    async def _source_call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        source = self.observation_source
        if source is None:
            return None
        method = getattr(source, name, None)
        if callable(method):
            return await _maybe_await(method(*args, **kwargs))
        if name == "observe" and callable(source):
            return await _maybe_await(source(*args, **kwargs))
        return None

    async def reset(self, context: Any) -> EnvironmentFeedback:
        self._reset_lifecycle(context)
        payload = await self._source_call("reset", context)
        if payload is None:
            payload = {"state": dict(self.initial_state), "info": {"source": "static"}}
        return self._coerce_feedback(payload, info={"reset": True})

    async def observe(self, request: Any = None) -> EnvironmentFeedback:
        self._ensure_open()
        payload = await self._source_call("observe", request)
        if payload is None and self.latest_feedback is not None:
            return self.latest_feedback
        if payload is None:
            payload = {"state": dict(self.initial_state), "info": {"source": "static"}}
        return self._coerce_feedback(payload)

    def _coerce_feedback(
        self,
        payload: Any,
        *,
        info: Mapping[str, Any] | None = None,
    ) -> EnvironmentFeedback:
        if isinstance(payload, EnvironmentFeedback):
            copy_model = getattr(payload, "model_copy", None)
            updates = {
                "session_id": self.session_id,
                "health": self._health_model(),
            }
            feedback = (
                copy_model(update=updates)
                if callable(copy_model)
                else payload.copy(update=updates)
            )
            return self._publish_feedback(feedback)
        if isinstance(payload, Observation):
            return self._new_feedback(
                observation=payload,
                state=payload.state,
                info=info,
            )
        if not isinstance(payload, Mapping):
            payload = {"state": {"value": payload}}
        value = dict(payload)
        if "io" in value:
            from ..schemas import EnvironmentIO
            # Persist the endpoint declaration across subsequent feedbacks.
            self.spec.parameters["environment_io"] = EnvironmentIO.model_validate(value["io"]).model_dump()
        observation_value = value.get("observation")
        if isinstance(observation_value, Observation):
            observation = observation_value
        elif isinstance(observation_value, Mapping):
            observation = _model_validate(Observation, observation_value)
        else:
            observation = Observation(
                frames=_frames_from_value(value.get("frames")),
                state=dict(value.get("state") or {}),
            )
        merged_info = dict(value.get("info") or {})
        merged_info.update(dict(info or {}))
        status = value.get("action_status", ActionStatus.UNKNOWN)
        if not isinstance(status, ActionStatus):
            status = ActionStatus(str(status))
        return self._new_feedback(
            observation=observation,
            state=dict(value.get("state") or observation.state),
            signals=_signals_from_value(value.get("signals")),
            reward=value.get("reward"),
            info=merged_info,
            terminal=bool(value.get("terminal", False)),
            success=value.get("success"),
            metadata=dict(value.get("metadata") or {}),
            status=status,
        )

    async def _close_impl(self, reason: str | None = None) -> None:
        await self._source_call("close", reason)


@register_environment("real_robot", replace=True)
class RealRobotAdapter(BaseEnvironmentAdapter):
    """Configurable physical-robot adapter with no vendor SDK import."""

    descriptor = EnvironmentDescriptor(
        env_id="real_robot",
        env_version="0.1.0",
        display_name="Real Robot",
        capabilities={"reset", "observe", "command", "physical_execution"},
        observation_modalities={"rgb", "proprioception", "state", "tactile"},
        vendor="site-provided ROS2 / SDK / RPC",
        backend_kind="remote",
        metadata={"action_execution": "real_robot_body"},
    )

    def __init__(
        self,
        *,
        robot_name: str = "robot0",
        observation_source: Any = None,
        initial_state: Mapping[str, Any] | None = None,
        **_: Any,
    ) -> None:
        super().__init__()
        self.robot_name = robot_name
        self.observation_source = observation_source
        self.initial_state = dict(initial_state or {})

    async def _connect_impl(self) -> None:
        source = self.observation_source
        connect = getattr(source, "connect", None) if source is not None else None
        if callable(connect):
            await _maybe_await(connect())

    def _create_session(self, spec: SessionSpec) -> RealRobotSession:
        return RealRobotSession(
            spec,
            robot_name=self.robot_name,
            observation_source=self.observation_source,
            initial_state=self.initial_state,
        )

    async def health(self) -> EnvironmentHealth:
        if self.observation_source is None:
            return EnvironmentHealth(
                ready=False,
                degraded=True,
                message="observation_source_not_configured",
            )
        return await super().health()


__all__ = ["RealRobotAdapter", "RealRobotSession"]
