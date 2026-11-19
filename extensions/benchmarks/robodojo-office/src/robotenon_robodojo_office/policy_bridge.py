"""Explicit adapter between XPolicyLab calls and a RoboTenon policy executor.

The bridge owns protocol state only. Model loading, graph execution and action
semantics belong to the injected executor, which keeps this transport adapter
independent from a particular VLA or graph configuration.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class PolicyExecutor(Protocol):
    """Policy-side operations required by the XPolicyLab bridge."""

    async def reset(self, *, trial_id: str | None = None) -> None: ...

    async def infer(
        self,
        observation: Mapping[str, Any],
        *,
        env_index: int | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...

    async def trial_end(self, result: Mapping[str, Any] | None = None) -> None: ...


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(slots=True)
class _EpisodeState:
    observations: dict[int, Mapping[str, Any]] = field(default_factory=dict)


class RoboTenonPolicyBridge:
    """Translate XPolicyLab's split update/infer API to one explicit executor."""

    def __init__(self, executor: PolicyExecutor) -> None:
        self.executor = executor
        self._state = _EpisodeState()

    async def reset(self) -> None:
        self._state = _EpisodeState()
        await _await(self.executor.reset())

    async def update_obs(self, obs: Mapping[str, Any]) -> None:
        if not isinstance(obs, Mapping):
            raise TypeError("RoboDojo observation must be a mapping")
        self._state.observations[0] = obs

    async def update_obs_batch(self, obs_list: Sequence[Mapping[str, Any]]) -> None:
        if not isinstance(obs_list, Sequence):
            raise TypeError("RoboDojo observation batch must be a sequence")
        updated: dict[int, Mapping[str, Any]] = {}
        for position, observation in enumerate(obs_list):
            if not isinstance(observation, Mapping):
                raise TypeError("each RoboDojo batch observation must be a mapping")
            env_index = int(observation.get("env_idx", position))
            updated[env_index] = observation
        self._state.observations.update(updated)

    async def get_action(self) -> list[Mapping[str, Any]]:
        observation = self._require_observation(0)
        actions = await _await(self.executor.infer(observation, env_index=0))
        return self._validate_actions(actions)

    async def get_action_batch(self, env_idx_list=None) -> list[list[Mapping[str, Any]]]:
        indices = list(env_idx_list or sorted(self._state.observations))
        return [
            self._validate_actions(
                await _await(
                    self.executor.infer(self._require_observation(int(index)), env_index=int(index))
                )
            )
            for index in indices
        ]

    async def prepare_case(self, case_meta=None) -> None:
        # The executor may optionally consume benchmark metadata without
        # making it a mandatory part of the common policy contract.
        method = getattr(self.executor, "prepare_case", None)
        if callable(method):
            await _await(method(dict(case_meta or {})))

    async def on_trial_end(self, result=None) -> None:
        await _await(self.executor.trial_end(dict(result or {})))

    def _require_observation(self, env_index: int) -> Mapping[str, Any]:
        try:
            return self._state.observations[env_index]
        except KeyError as exc:
            raise RuntimeError(
                f"get_action called before update_obs for env_index={env_index}"
            ) from exc

    @staticmethod
    def _validate_actions(actions: Any) -> list[Mapping[str, Any]]:
        if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
            raise TypeError("policy executor must return an action sequence")
        result = list(actions)
        if not result or any(not isinstance(action, Mapping) for action in result):
            raise ValueError("policy executor returned an empty or invalid action chunk")
        return result


__all__ = ["PolicyExecutor", "RoboTenonPolicyBridge"]
