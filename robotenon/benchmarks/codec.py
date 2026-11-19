"""Lossless native observations in RoboTenon's serializable feedback envelope."""
from __future__ import annotations

from collections.abc import Mapping
import numpy as np

from ..schemas import Observation, ObservationFrame


def pack_observation(value: Mapping) -> Observation:
    frames = []

    def encode(item, path=()):
        if isinstance(item, np.ndarray):
            if item.dtype.hasobject:
                raise TypeError("object arrays are not benchmark observations")
            frame_id = f"native_{len(frames)}"
            is_image = bool(path and path[0] in {"images", "video_history"})
            frames.append(ObservationFrame(
                frame_id=frame_id, role="policy_input",
                modality="rgb" if is_image else "state", producer="benchmark",
                camera_id=frame_id if is_image else None,
                shape=list(item.shape), encoding="npy", data=item.copy(),
                metadata={"native_path": list(path), "native_dtype": str(item.dtype)},
            ))
            return {"$frame": frame_id}
        if isinstance(item, np.generic):
            return item.item()
        if isinstance(item, Mapping):
            return {"$dict": [[str(k), encode(v, (*path, str(k)))] for k, v in item.items()]}
        if isinstance(item, (list, tuple)):
            return {"$tuple" if isinstance(item, tuple) else "$list": [
                encode(v, (*path, i)) for i, v in enumerate(item)]}
        if item is None or isinstance(item, (bool, str, int, float)):
            return item
        raise TypeError(f"unsupported native observation {type(item).__name__} at {path}")

    encoded = encode(value)
    return Observation(state={"native_observation": encoded}, frames=frames)


def unpack_observation(observation: Observation, *, resolver=None):
    frames = {frame.frame_id: frame for frame in observation.frames}

    def decode(item):
        if not isinstance(item, dict):
            return item
        if "$frame" in item:
            frame = frames[item["$frame"]]
            data = frame.data
            if data is None and resolver is not None:
                data = resolver(frame)
            if data is None:
                raise ValueError(f"native observation frame {frame.frame_id} is not resolved")
            return np.asarray(data, dtype=frame.metadata["native_dtype"]).reshape(frame.shape).copy()
        if "$dict" in item:
            return {k: decode(v) for k, v in item["$dict"]}
        if "$tuple" in item:
            return tuple(decode(v) for v in item["$tuple"])
        return [decode(v) for v in item["$list"]]

    return decode(observation.state["native_observation"])


def json_value(value):
    """Strict JSON values for small actions/metrics; never silently stringify."""
    if isinstance(value, np.ndarray):
        return json_value(value.tolist())
    if isinstance(value, np.generic):
        return json_value(value.item())
    if isinstance(value, Mapping):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, float) and not np.isfinite(value):
        raise ValueError("non-finite benchmark value")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"non-serializable benchmark value: {type(value).__name__}")
