"""Optional MolmoSpaces runtime controls kept outside the migrated source."""
from __future__ import annotations

import json
from pathlib import Path

from ._internal.upstream_benchmark import MolmoSpacesBenchmark as NativeMolmoSpacesBenchmark


class MolmoSpacesBenchmark(NativeMolmoSpacesBenchmark):
    """Native benchmark with an optional resource-version manifest."""

    def __init__(self, *args, resource_manifest=None, **kwargs):
        self._configure_resource_versions(resource_manifest)
        super().__init__(*args, **kwargs)

    @staticmethod
    def _configure_resource_versions(resource_manifest) -> None:
        """Apply one version map before MolmoSpaces evaluation modules import."""
        if resource_manifest is None:
            return
        if isinstance(resource_manifest, str):
            manifest = json.loads(Path(resource_manifest).read_text())
        elif isinstance(resource_manifest, dict):
            manifest = resource_manifest
        else:
            raise TypeError("resource_manifest must be a path, mapping, or None")
        if not manifest or not all(
            isinstance(kind, str)
            and isinstance(sources, dict)
            and all(
                isinstance(source, str) and isinstance(version, str)
                for source, version in sources.items()
            )
            for kind, sources in manifest.items()
        ):
            raise ValueError(
                "resource_manifest must map data types and sources to version strings"
            )
        from molmo_spaces import molmo_spaces_constants as constants

        constants.DATA_TYPE_TO_SOURCE_TO_VERSION.clear()
        constants.DATA_TYPE_TO_SOURCE_TO_VERSION.update(manifest)
