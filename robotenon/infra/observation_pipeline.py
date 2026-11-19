"""Adapter-neutral observation normalisation and cache ingestion."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from ..schemas import EnvironmentFeedback, ObservationFrame, ObservationRef
from ..contracts.persistence import ObservationStore
from .observation_resolver import ObservationResolver


def _coerce_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return uuid5(NAMESPACE_URL, f"robotenon:session:{value}")


def _now_ns() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


def _metadata_safe(value: Any) -> Any:
    """Keep ctx metadata JSON-safe and avoid retaining sensor-sized buffers."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"omitted": True, "type": "bytes", "size_bytes": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _metadata_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_metadata_safe(item) for item in value]
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    if shape is not None or dtype is not None:
        nbytes = getattr(value, "nbytes", None)
        return {"omitted": True, "type": type(value).__name__, "shape": list(shape or ()), "dtype": str(dtype) if dtype is not None else None, "size_bytes": int(nbytes) if nbytes is not None else None}
    return str(value)


def _contains_large(value: Any) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return True
    if getattr(value, "nbytes", None) is not None:
        return True
    if isinstance(value, Mapping):
        return any(_contains_large(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_large(item) for item in value)
    return False


def _infer_modality(name: str, value: Any = None) -> str:
    text = str(name).lower()
    if any(token in text for token in ("depth", "range")):
        return "depth"
    if any(token in text for token in ("pointcloud", "point_cloud", "pcd", "lidar")):
        return "pointcloud"
    if any(token in text for token in ("proprio", "joint", "state", "pose", "velocity", "effort")):
        return "proprioception"
    if any(token in text for token in ("text", "caption", "instruction")):
        return "text"
    if isinstance(value, str):
        suffix = os.path.splitext(value.split("?", 1)[0])[1].lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
            return "rgb"
        return "text"
    if any(token in text for token in ("audio", "sound", "mic")):
        return "audio"
    return "rgb" if any(token in text for token in ("camera", "image", "rgb", "front", "wrist")) else "other"


class ObservationPipeline:
    """Convert raw environment feedback to URI-only canonical feedback."""

    def __init__(self, cache: ObservationStore, *, resolver: ObservationResolver | None = None):
        self.cache = cache
        self.resolver = resolver or ObservationResolver(cache)
        # Sequence/epoch ordering only needs to be serialised per session.
        # A single global lock made an unrelated robot/session wait behind a
        # slow normalisation or cache enqueue from another session.
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._epoch_by_session: dict[str, int] = {}
        self._sequence_by_session_epoch: dict[tuple[str, int], int] = {}
        # Some SDKs return the same feedback object from a read-only observe()
        # call.  The pipeline still represents each observation event as one
        # cache record, so a reused producer id is replaced when its assigned
        # (epoch, sequence) changes.
        self._feedback_seen: OrderedDict[str, tuple[int, int]] = OrderedDict()
        # Most deployments bind one environment session to a process.  Keep
        # the last session hint so an adapter that omits session_id after its
        # first reset does not accidentally create a new random session on
        # every observe/step.  Multi-session callers can always pass an
        # explicit session_id and remain isolated.
        self._last_session_hint: Any = None

    def _lock_for_session(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    @staticmethod
    def normalize_feedback(
        raw_feedback: EnvironmentFeedback | Mapping[str, Any],
        *,
        session_id: Any = None,
        epoch: int = 0,
        sequence: int | None = None,
    ) -> EnvironmentFeedback:
        """Create canonical feedback while accepting lightweight adapter dicts."""

        if isinstance(raw_feedback, EnvironmentFeedback):
            extra_values = getattr(raw_feedback, "model_extra", None)
            if extra_values is None:
                extra_values = {
                    key: value
                    for key, value in getattr(raw_feedback, "__dict__", {}).items()
                    if key not in getattr(EnvironmentFeedback, "__fields__", {})
                }
            if extra_values:
                metadata = dict(raw_feedback.metadata or {})
                metadata.setdefault("_adapter_extra", _metadata_safe(extra_values))
                field_names = set(
                    getattr(EnvironmentFeedback, "model_fields", {})
                    or getattr(EnvironmentFeedback, "__fields__", {})
                )
                data = {
                    key: getattr(raw_feedback, key)
                    for key in field_names
                    if hasattr(raw_feedback, key)
                }
                data["metadata"] = metadata
                try:
                    return EnvironmentFeedback.model_validate(data)
                except Exception:
                    # A plugin may have populated a raw nested frame that
                    # Pydantic cannot revalidate without the cache.  The
                    # cache boundary still strips it later; preserve the
                    # original object rather than rejecting the observation.
                    return raw_feedback.model_copy(update={"metadata": metadata})
            return raw_feedback
        if not isinstance(raw_feedback, Mapping):
            raise TypeError("environment feedback must be EnvironmentFeedback or a mapping")
        raw = dict(raw_feedback)
        field_names = set(
            getattr(EnvironmentFeedback, "model_fields", {})
            or getattr(EnvironmentFeedback, "__fields__", {})
        )
        # Pydantic models allow extra fields for backwards compatibility.  A
        # lightweight adapter often puts a raw camera/state value at the
        # top-level, though; keep those values available to ``_extract`` via
        # ``raw_payload`` but move only bounded summaries into metadata before
        # constructing the serialisable feedback object.
        extras = {
            key: raw.pop(key)
            for key in list(raw)
            if key not in field_names
        }
        if extras:
            metadata = dict(raw.get("metadata") or {})
            metadata.setdefault("_adapter_extra", _metadata_safe(extras))
            raw["metadata"] = metadata
        original_session = raw.get("session_id") or session_id
        original_feedback = raw.get("feedback_id")
        raw["session_id"] = _coerce_uuid(original_session or uuid4())
        if original_feedback is None:
            raw["feedback_id"] = uuid4()
        else:
            try:
                raw["feedback_id"] = UUID(str(original_feedback))
            except (ValueError, TypeError, AttributeError):
                raw["feedback_id"] = uuid5(NAMESPACE_URL, f"robotenon:feedback:{original_feedback}")
        if sequence is not None:
            raw.setdefault("sequence", sequence)
            raw.setdefault("sequence_no", sequence)
        elif "sequence_no" not in raw and "sequence" in raw:
            raw["sequence_no"] = raw["sequence"]
        raw.setdefault("sequence_no", 0)
        raw.setdefault("sequence", raw["sequence_no"])
        raw.setdefault("epoch", epoch)
        raw.setdefault("timestamp_ns", _now_ns())
        if not isinstance(raw.get("state"), Mapping):
            value = _metadata_safe(raw.get("state")) if raw.get("state") is not None else {}
            raw["state"] = value if isinstance(value, Mapping) else {"value": value}
        if not isinstance(raw.get("info"), Mapping):
            value = _metadata_safe(raw.get("info")) if raw.get("info") is not None else {}
            raw["info"] = value if isinstance(value, Mapping) else {"value": value}
        if isinstance(raw.get("state"), Mapping):
            raw["state"] = _metadata_safe(raw["state"])
        if isinstance(raw.get("info"), Mapping):
            raw["info"] = _metadata_safe(raw["info"])
        if original_session is not None:
            metadata = dict(raw.get("metadata") or {})
            try:
                UUID(str(original_session))
            except (ValueError, TypeError, AttributeError):
                metadata.setdefault("_cache_session_id", str(original_session))
            raw["metadata"] = metadata
        if original_feedback is not None:
            metadata = dict(raw.get("metadata") or {})
            try:
                UUID(str(original_feedback))
            except (ValueError, TypeError, AttributeError):
                metadata.setdefault("_cache_feedback_id", str(original_feedback))
            raw["metadata"] = metadata

        # Raw frame maps are kept outside the nested Observation model until
        # ``ingest`` converts each value to an ObservationRef.  This allows an
        # adapter to return ``{"frames": {"front_camera": ndarray}}``.
        raw.pop("frames", None)
        observation = raw.get("observation")
        if isinstance(observation, Mapping):
            observation_copy = dict(observation)
            observation_copy.pop("frames", None)
            raw["observation"] = observation_copy
        return EnvironmentFeedback.model_validate(raw)

    @staticmethod
    def _descriptor(value: Any, *, name: str) -> tuple[Any, dict[str, Any]]:
        if isinstance(value, ObservationRef):
            return value, {}
        if isinstance(value, Mapping) and "uri" in value and "data" not in value and "payload" not in value:
            return ObservationRef.model_validate(value), {}
        if isinstance(value, Mapping) and ("data" in value or "payload" in value):
            payload = value.get("data", value.get("payload"))
            metadata = {
                key: value[key]
                for key in ("modality", "media_type", "encoding", "shape", "dtype")
                if key in value
            }
            metadata.setdefault("modality", _infer_modality(name, payload))
            return payload, metadata
        return value, {"modality": _infer_modality(name, value)}

    def _extract(self, raw_payload: Any, feedback: EnvironmentFeedback) -> tuple[dict[str, Any], dict[str, ObservationRef]]:
        payloads: dict[str, Any] = {}
        refs: dict[str, ObservationRef] = dict(feedback.frames or {})

        def add(name: str, value: Any) -> None:
            payload, descriptor = self._descriptor(value, name=name)
            if isinstance(payload, ObservationRef):
                refs[name] = payload
                return
            payloads[name] = {"data": payload, **descriptor}

        if isinstance(raw_payload, Mapping):
            frame_source = raw_payload.get("frames")
            if frame_source is None and isinstance(raw_payload.get("observation"), Mapping):
                frame_source = raw_payload["observation"].get("frames")
            if isinstance(frame_source, Mapping):
                for name, value in frame_source.items():
                    add(str(name), value)
            elif isinstance(frame_source, (list, tuple)):
                for index, value in enumerate(frame_source):
                    if isinstance(value, ObservationFrame):
                        name = value.camera_id or value.frame_id or f"frame_{index}"
                        descriptor = {
                            "data": value.data,
                            "modality": value.modality,
                            "encoding": value.encoding,
                            "shape": value.shape,
                        }
                        if value.artifact_ref and value.artifact_ref.get("uri") and value.data is None:
                            artifact = dict(value.artifact_ref)
                            allowed = {
                                "uri", "modality", "media_type", "encoding", "shape", "dtype",
                                "timestamp_ns", "feedback_id", "sequence", "sha256", "size_bytes", "metadata",
                            }
                            artifact = {key: item for key, item in artifact.items() if key in allowed}
                            artifact.setdefault("uri", value.artifact_ref["uri"])
                            artifact.setdefault("modality", value.modality)
                            refs[name] = ObservationRef.model_validate(artifact)
                        else:
                            add(name, descriptor)
                    elif isinstance(value, Mapping):
                        name = str(value.get("name") or value.get("camera_id") or value.get("frame_id") or f"frame_{index}")
                        add(name, value)
                    else:
                        add(f"frame_{index}", value)
            # A direct mapping of frame names is convenient for tiny adapters.
            if frame_source is None:
                reserved = {"feedback_id", "session_id", "epoch", "sequence", "sequence_no", "timestamp", "timestamp_ns", "observation", "state", "reward", "terminal", "success", "info", "metadata", "signals", "health", "action_id", "action_status", "safety_level", "task", "task_id", "environment_id", "run_id", "episode_id", "experiment_id", "source"}
                for name, value in raw_payload.items():
                    modality_name = _infer_modality(str(name), value)
                    path_like = isinstance(value, (str, os.PathLike)) and (
                        str(value).startswith(("http://", "https://", "file://"))
                        or os.path.splitext(str(value).split("?", 1)[0])[1].lower()
                        in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".mp4", ".npy", ".ply", ".pcd"}
                    )
                    if name not in reserved and (
                        not isinstance(value, (str, int, float, bool, type(None)))
                        or modality_name != "other"
                        or path_like
                    ):
                        add(str(name), value)
        else:
            # Pydantic's extra fields are not part of Mapping iteration.  Read
            # them here as another adapter-friendly shorthand for raw frames.
            extras = getattr(raw_payload, "model_extra", None)
            if extras is None:
                extras = {
                    key: value
                    for key, value in getattr(raw_payload, "__dict__", {}).items()
                    if key not in getattr(EnvironmentFeedback, "__fields__", {})
                }
            for name, value in (extras or {}).items():
                if not isinstance(value, (str, int, float, bool, type(None))):
                    add(str(name), value)

        for frame in feedback.observation.frames:
            name = frame.camera_id or frame.frame_id
            if frame.data is not None:
                add(name, {"data": frame.data, "modality": frame.modality, "encoding": frame.encoding, "shape": frame.shape})
            elif frame.artifact_ref and frame.artifact_ref.get("uri"):
                artifact = dict(frame.artifact_ref)
                allowed = {
                    "uri", "modality", "media_type", "encoding", "shape", "dtype",
                    "timestamp_ns", "feedback_id", "sequence", "sha256", "size_bytes", "metadata",
                }
                artifact = {key: item for key, item in artifact.items() if key in allowed}
                artifact.setdefault("uri", frame.artifact_ref["uri"])
                artifact.setdefault("modality", frame.modality)
                refs[name] = ObservationRef.model_validate(artifact)
        for channel in feedback.observation.channels:
            if channel.uri:
                refs.setdefault(
                    channel.name,
                    ObservationRef(
                        uri=channel.uri,
                        modality=channel.modality,
                        encoding=channel.encoding,
                        shape=channel.shape,
                        timestamp_ns=int(channel.timestamp.timestamp() * 1_000_000_000),
                        feedback_id=str(
                            (feedback.metadata or {}).get(
                                "_cache_feedback_id", str(feedback.feedback_id)
                            )
                        ),
                        sequence=feedback.sequence_no,
                        metadata=dict(channel.metadata),
                    ),
                )
        state_value = raw_payload.get("state") if isinstance(raw_payload, Mapping) else feedback.state
        if _contains_large(state_value) and "proprioception" not in payloads and "proprioception" not in refs:
            add("proprioception", {"data": state_value, "modality": "proprioception", "encoding": "json"})
        return payloads, refs

    async def ingest(
        self,
        feedback: EnvironmentFeedback | Mapping[str, Any],
        *,
        raw_payload: Any = None,
        session_id: Any = None,
        epoch: int | None = None,
        sequence: int | None = None,
        reset: bool = False,
        durable: bool = False,
    ) -> EnvironmentFeedback:
        """Ingest one reset/observe/step result and return immediately by default."""

        if session_id is None:
            raw_session = (
                feedback.get("session_id")
                if isinstance(feedback, Mapping)
                else getattr(feedback, "session_id", None)
            )
            if not raw_session and self._last_session_hint is not None:
                session_id = self._last_session_hint
        canonical = self.normalize_feedback(feedback, session_id=session_id, epoch=epoch or 0, sequence=sequence)
        sid = str(canonical.session_id)
        async with self._lock_for_session(sid):
            previous_epoch = self._epoch_by_session.get(sid)
            target_epoch = int(canonical.epoch if epoch is None else epoch)
            if reset:
                target_epoch = max(target_epoch, (previous_epoch + 1) if previous_epoch is not None else target_epoch)
            if previous_epoch is not None and not reset and target_epoch < previous_epoch:
                target_epoch = previous_epoch
            key = (sid, target_epoch)
            previous_sequence = self._sequence_by_session_epoch.get(key, -1)
            target_sequence = sequence if sequence is not None else canonical.sequence_no
            if reset:
                target_sequence = 0
            elif target_sequence <= previous_sequence and str(canonical.feedback_id):
                target_sequence = previous_sequence + 1
            self._epoch_by_session[sid] = target_epoch
            self._sequence_by_session_epoch[key] = int(target_sequence)
            if reset and previous_epoch is not None:
                clear_hot = getattr(self.cache, "clear_hot", None)
                if callable(clear_hot):
                    result = clear_hot(sid, epoch=previous_epoch)
                    if asyncio.iscoroutine(result):
                        await result
            canonical = canonical.model_copy(
                update={
                    "epoch": target_epoch,
                    "sequence": int(target_sequence),
                    "sequence_no": int(target_sequence),
                    "timestamp_ns": (
                        canonical.timestamp_ns
                        if canonical.timestamp_ns is not None
                        else _now_ns()
                    ),
                    "state": _metadata_safe(canonical.state),
                    "info": _metadata_safe(canonical.info),
                    "metadata": _metadata_safe(canonical.metadata),
                    "observation": canonical.observation.model_copy(
                        update={"state": _metadata_safe(canonical.observation.state)}
                    ),
                }
            )
            source_feedback_id = str(canonical.feedback_id)
            seen_at = self._feedback_seen.get(source_feedback_id)
            uri_only_feedback = bool(canonical.frames) or any(
                channel.uri for channel in canonical.observation.channels
            ) or any(
                frame.artifact_ref and frame.artifact_ref.get("uri")
                for frame in canonical.observation.frames
            )
            if (
                seen_at is not None
                and seen_at != (target_epoch, int(target_sequence))
                and not uri_only_feedback
            ):
                metadata = dict(canonical.metadata or {})
                logical_id = metadata.pop("_cache_feedback_id", None)
                if logical_id:
                    metadata["_cache_feedback_id"] = f"{logical_id}-{int(target_sequence)}"
                canonical = canonical.model_copy(
                    update={"feedback_id": uuid4(), "metadata": metadata}
                )
            canonical_feedback_key = str(canonical.feedback_id)
            self._feedback_seen.pop(canonical_feedback_key, None)
            self._feedback_seen[canonical_feedback_key] = (
                target_epoch,
                int(target_sequence),
            )
            while len(self._feedback_seen) > 4096:
                self._feedback_seen.popitem(last=False)
            payload_source = raw_payload if raw_payload is not None else feedback
            payloads, refs = self._extract(payload_source, canonical)
            # Seed known refs before cache.append creates refs for payloads.
            canonical = canonical.model_copy(update={"frames": refs})
            result = await self.cache.append(canonical, payloads=payloads, durable=durable)
            self._last_session_hint = result.metadata.get(
                "_cache_session_id", result.session_id
            )
            # The cache strips data from Observation.frames and writes URI
            # channels.  Keep the result's frame refs as the sole payload view.
            clean_frames: list[ObservationFrame] = []
            for frame in result.observation.frames:
                name = frame.camera_id or frame.frame_id
                ref = result.frames.get(name)
                if ref is not None:
                    clean_frames.append(frame.model_copy(update={"data": None, "artifact_ref": ref.model_dump(mode="json")}))
                else:
                    clean_frames.append(frame.model_copy(update={"data": None}))
            if clean_frames:
                result = result.model_copy(update={"observation": result.observation.model_copy(update={"frames": clean_frames})})
            return result


__all__ = ["ObservationPipeline"]
