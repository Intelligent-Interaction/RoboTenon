"""Resolve logical observation references at the model/tool boundary."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..schemas import EnvironmentFeedback, ObservationRef


@dataclass(slots=True)
class ObservationView:
    """Runtime view of one feedback with decoded frame values.

    The view is intentionally ephemeral.  It may contain bytes/ndarrays for a
    model call, while the originating ``GraphContext`` still contains only
    ``ObservationRef`` metadata and logical URIs.
    """

    feedback: EnvironmentFeedback
    frames: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, ObservationRef] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    reward: float | None = None
    terminal: bool = False
    info: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, name: str) -> Any:
        return self.frames[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self.frames.get(name, default)

    def keys(self):
        return self.frames.keys()

    def items(self):
        return self.frames.items()

    def values(self):
        return self.frames.values()


class ObservationResolver:
    """Decode ``cache://`` refs using one resident cache instance."""

    def __init__(self, cache: Any = None):
        self.cache = cache

    async def resolve(self, uri: str) -> Any:
        if self.cache is None:
            from .observation_store import payload_to_bytes

            raw = await __import__("asyncio").to_thread(payload_to_bytes, uri)
            return self._decode(raw, {"uri": uri})
        raw, metadata = await self.cache.read_uri_bytes(uri)
        return self._decode(raw, metadata)

    async def resolve_ref(self, ref: ObservationRef | Mapping[str, Any]) -> Any:
        if not isinstance(ref, ObservationRef):
            ref = ObservationRef.model_validate(ref)
        raw, metadata = await self.cache.read_uri_bytes(ref.uri) if self.cache is not None else (None, {})
        if raw is None:
            from .observation_store import payload_to_bytes
            raw = await __import__("asyncio").to_thread(payload_to_bytes, ref.uri)
        combined = dict(ref.model_dump(mode="json"))
        combined.update(metadata)
        return self._decode(raw, combined)

    async def resolve_feedback(self, feedback: EnvironmentFeedback) -> ObservationView:
        if not isinstance(feedback, EnvironmentFeedback):
            feedback = EnvironmentFeedback.model_validate(feedback)
        refs = dict(feedback.frames or {})
        # Older adapters may populate Observation.channels before the cache
        # pipeline is enabled.  URI channels are still resolvable.
        for channel in feedback.observation.channels:
            if channel.uri and channel.name not in refs:
                refs[channel.name] = ObservationRef(
                    uri=channel.uri,
                    modality=channel.modality,
                    encoding=channel.encoding,
                    shape=channel.shape,
                    timestamp_ns=int(channel.timestamp.timestamp() * 1_000_000_000),
                    feedback_id=str(feedback.feedback_id),
                    sequence=feedback.sequence_no,
                    metadata=dict(channel.metadata),
                )
        names = list(refs)
        values = await __import__("asyncio").gather(*(self.resolve_ref(refs[name]) for name in names)) if names else []
        return ObservationView(
            feedback=feedback,
            frames=dict(zip(names, values)),
            refs=refs,
            state=dict(feedback.state or feedback.observation.state or {}),
            reward=feedback.reward,
            terminal=feedback.terminal,
            info=dict(feedback.info or feedback.metadata or {}),
        )

    @staticmethod
    def _decode(raw: bytes, metadata: Mapping[str, Any]) -> Any:
        media_type = str(metadata.get("media_type") or "").lower()
        encoding = str(metadata.get("encoding") or "").lower()
        if media_type in {"application/json", "text/json"} or encoding == "json":
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return raw
        if media_type.startswith("text/") or encoding in {"utf8", "utf-8", "text"}:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw
        if media_type == "application/x-npy" or encoding == "npy" or str(metadata.get("uri", "")).endswith(".npy"):
            try:
                import numpy as np  # type: ignore[import-not-found]

                return np.load(io.BytesIO(raw), allow_pickle=False)
            except Exception:
                return raw
        # Image/video/point-cloud bytes are intentionally not eagerly decoded:
        # model providers commonly accept encoded bytes and this keeps Pillow,
        # OpenCV and PCL optional.  A provider can inspect ``media_type`` in
        # the accompanying ref when it needs an ndarray conversion.
        return raw


__all__ = ["ObservationResolver", "ObservationView"]
