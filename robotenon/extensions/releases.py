from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field

from ..errors import ReleaseError
from ..schemas import RoboModel


class ReleaseStatus(str, Enum):
    DRAFT = "draft"
    VALIDATING = "validating"
    CANDIDATE = "candidate"
    RELEASED = "released"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


class ExperimentRelease(RoboModel):
    release_id: str
    name: str
    version: str
    status: ReleaseStatus = ReleaseStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    config: dict[str, Any] = Field(default_factory=dict)
    model_artifacts: list[str] = Field(default_factory=list)
    env_requirements: dict[str, Any] = Field(default_factory=dict)
    checks: dict[str, str] = Field(default_factory=dict)
    content_hash: str = ""

    def compute_hash(self) -> str:
        data = self.model_dump(mode="json", exclude={"status", "created_at", "content_hash"})
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ExperimentReleaseRegistry:
    """Filesystem registry for reproducible experiment versions."""

    _allowed = {
        ReleaseStatus.DRAFT: {ReleaseStatus.VALIDATING, ReleaseStatus.REVOKED},
        ReleaseStatus.VALIDATING: {ReleaseStatus.CANDIDATE, ReleaseStatus.DRAFT, ReleaseStatus.REVOKED},
        ReleaseStatus.CANDIDATE: {ReleaseStatus.RELEASED, ReleaseStatus.DRAFT, ReleaseStatus.REVOKED},
        ReleaseStatus.RELEASED: {ReleaseStatus.DEPRECATED, ReleaseStatus.REVOKED},
        ReleaseStatus.DEPRECATED: {ReleaseStatus.REVOKED},
        ReleaseStatus.REVOKED: set(),
    }

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, release: ExperimentRelease) -> ExperimentRelease:
        release.content_hash = release.compute_hash()
        (self.root / f"{release.name}-{release.version}.json").write_text(release.model_dump_json(indent=2), encoding="utf-8")
        return release

    def get(self, name: str, version: str) -> ExperimentRelease:
        path = self.root / f"{name}-{version}.json"
        if not path.exists():
            raise ReleaseError(f"release not found: {name}:{version}")
        return ExperimentRelease.model_validate_json(path.read_text(encoding="utf-8"))

    def transition(self, name: str, version: str, status: ReleaseStatus, *, check: str | None = None) -> ExperimentRelease:
        release = self.get(name, version)
        if status not in self._allowed[release.status]:
            raise ReleaseError(f"invalid release transition {release.status.value} -> {status.value}")
        if status == ReleaseStatus.CANDIDATE and not release.model_artifacts:
            raise ReleaseError("candidate release must reference at least one model artifact")
        release.status = status
        if check:
            release.checks[check] = "passed"
        return self.put(release)

    def list(self) -> list[ExperimentRelease]:
        return [ExperimentRelease.model_validate_json(path.read_text(encoding="utf-8")) for path in sorted(self.root.glob("*.json"))]
