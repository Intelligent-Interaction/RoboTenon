"""Reference executors for validating the official transport integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ZeroJointExecutor:
    """Valid dual-X5 action producer for smoke tests, not a benchmark policy."""

    async def reset(self, *, trial_id: str | None = None) -> None:
        return None

    async def infer(self, observation: Mapping[str, Any], *, env_index=None):
        return [{
            "left_arm_joint_state": [0.0] * 6,
            "left_ee_joint_state": [0.0],
            "right_arm_joint_state": [0.0] * 6,
            "right_ee_joint_state": [0.0],
        }]

    async def trial_end(self, result=None) -> None:
        return None

    async def close(self) -> None:
        return None


def create_zero_executor(config: dict[str, Any]) -> ZeroJointExecutor:
    return ZeroJointExecutor()


__all__ = ["ZeroJointExecutor", "create_zero_executor"]
