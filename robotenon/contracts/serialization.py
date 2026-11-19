"""Strict JSON encoding used by durable state and service messages."""

from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
import math
from pathlib import Path
from typing import Any
from uuid import UUID


def jsonable(value: Any) -> Any:
    """Convert common RoboTenon/Python values to JSON-compatible values."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floating point values are not JSON-safe")
        return value
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, (datetime, date, UUID, Path)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return jsonable(dump(mode="json"))
        except TypeError:
            return jsonable(dump())
    dump = getattr(value, "dict", None)
    if callable(dump):
        return jsonable(dump())
    raise TypeError(f"unsupported checkpoint value: {type(value).__name__}")
