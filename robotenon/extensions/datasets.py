from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from ..schemas import DatasetSnapshot, RunManifest


class DatasetBuilder:
    """Create immutable dataset manifests from successful episode manifests."""

    def __init__(self, episodes_root: str | Path, output_root: str | Path):
        self.episodes_root = Path(episodes_root)
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)

    def build(self, *, dataset_id: str, version: str, predicate: Callable[[RunManifest], bool] | None = None,
              transform_version: str = "identity@1", split_seed: int = 0) -> DatasetSnapshot:
        manifests: list[RunManifest] = []
        for path in self.episodes_root.glob("**/manifest.json"):
            try:
                manifest = RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if manifest.status != "succeeded" or (predicate and not predicate(manifest)):
                continue
            manifests.append(manifest)
        manifests.sort(key=lambda item: str(item.run_id))
        ids = [item.run_id for item in manifests]
        train, valid, test = [], [], []
        for index, run_id in enumerate(ids):
            bucket = index % 10
            (test if bucket == 0 else valid if bucket == 1 else train).append(run_id)
        snapshot = DatasetSnapshot(dataset_id=dataset_id, version=version, source_run_ids=ids,
                                   split={"train": train, "validation": valid, "test": test},
                                   transform_version=transform_version, query={"split_seed": split_seed})
        payload = snapshot.model_dump(mode="json", exclude={"content_hash"})
        snapshot.content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        (self.output_root / f"{dataset_id}-{version}.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        return snapshot
