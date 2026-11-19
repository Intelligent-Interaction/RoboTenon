from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import Field

from ..errors import ReleaseError
from ..schemas import RoboModel
from .releases import ExperimentRelease


class DeploymentStatus(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    FAILED = "failed"


class DeploymentHealth(RoboModel):
    status: DeploymentStatus
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DeploymentTarget(Protocol):
    async def start(self, release: ExperimentRelease) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> DeploymentHealth: ...


class DeploymentManager:
    """Target-independent release/deploy/drain/rollback state machine."""

    def __init__(self, target: DeploymentTarget):
        self.target = target
        self.active: ExperimentRelease | None = None
        self.previous: ExperimentRelease | None = None
        self.status = DeploymentStatus.STOPPED

    async def deploy(self, release: ExperimentRelease) -> DeploymentHealth:
        if release.status.value != "released":
            raise ReleaseError("only a released experiment can be deployed")
        self.status = DeploymentStatus.STARTING
        try:
            await self.target.start(release)
            self.previous, self.active = self.active, release
            self.status = DeploymentStatus.READY
            return await self.target.health()
        except Exception:
            self.status = DeploymentStatus.FAILED
            raise

    async def drain(self) -> None:
        self.status = DeploymentStatus.DRAINING
        await self.target.stop()
        self.status = DeploymentStatus.STOPPED

    async def rollback(self) -> DeploymentHealth:
        if self.previous is None:
            raise ReleaseError("no previous release available for rollback")
        previous = self.previous
        await self.target.start(previous)
        self.active, self.previous = previous, self.active
        self.status = DeploymentStatus.READY
        return await self.target.health()
