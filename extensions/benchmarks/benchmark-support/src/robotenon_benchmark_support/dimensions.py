"""Extract extension-declared result dimensions from native tasks and parameters."""
from __future__ import annotations

import json
from collections.abc import Mapping


def _lookup(root, path):
    value = root
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _category(value):
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def extract_dimensions(spec, task, params):
    """Resolve ``{"label": "task.x" | "params.x"}`` into JSON categories."""
    dimensions = {}
    for label, reference in (spec or {}).items():
        if not isinstance(label, str) or not label or not isinstance(reference, str):
            raise ValueError("result dimension declarations must map names to paths")
        namespace, separator, path = reference.partition(".")
        if not separator or namespace not in {"task", "params"} or not path:
            raise ValueError(f"invalid result dimension path: {reference!r}")
        value = _lookup(task if namespace == "task" else params, path)
        value = _category(value)
        if value is not None:
            dimensions[label] = value
    return dimensions
