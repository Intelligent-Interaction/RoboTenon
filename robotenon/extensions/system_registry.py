from __future__ import annotations

from pathlib import Path

from ..schemas import SystemArtifact, SystemRelease


class SystemRegistry:
    """Filesystem registry for immutable system artifacts and releases."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def register_artifact(self, artifact: SystemArtifact) -> SystemArtifact:
        path = self.root / "artifacts" / f"{artifact.name}-{artifact.version}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = SystemArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            if existing.content_hash != artifact.content_hash:
                raise ValueError("artifact version already exists with a different content hash")
            return existing
        path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
        return artifact

    def register_release(self, release: SystemRelease) -> SystemRelease:
        if release.status == "released" and not release.validation_refs:
            raise ValueError("released system needs validation references")
        path = self.root / "releases" / f"{release.release_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise ValueError(f"system release already exists: {release.release_id}")
        path.write_text(release.model_dump_json(indent=2), encoding="utf-8")
        return release

    def get_release(self, release_id: str) -> SystemRelease:
        return SystemRelease.model_validate_json((self.root / "releases" / f"{release_id}.json").read_text(encoding="utf-8"))

    def list_releases(self) -> list[SystemRelease]:
        return [SystemRelease.model_validate_json(path.read_text(encoding="utf-8")) for path in sorted((self.root / "releases").glob("*.json"))]
