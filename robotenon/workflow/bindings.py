"""Explicit JSON data bindings, evaluated against committed invocation snapshots."""
from collections.abc import Mapping
from ..contracts.workspace import thaw

_MISSING = object()


def _parts(source):
    if source.startswith("nodes."):
        node, separator, path = source[6:].partition(".outputs.")
        if not separator or not node or not path:
            raise ValueError(f"invalid node output binding {source!r}")
        return ["nodes", node, "outputs", *path.split(".")]
    return source.split(".")


def normalize_binding(binding):
    value = {"source": binding} if isinstance(binding, str) else dict(binding)
    if set(value) - {"source", "default"} or "source" not in value:
        raise ValueError("binding requires source and optional default")
    sources = value["source"] if isinstance(value["source"], list) else [value["source"]]
    if not sources or any(not isinstance(source, str) for source in sources):
        raise ValueError("binding source must be a path or ordered fallback paths")
    for source in sources:
        parts = _parts(source)
        if parts[0] == "nodes":
            if len(parts) < 4 or parts[2] != "outputs":
                raise ValueError(f"invalid binding path {source!r}; use nodes.<id>.outputs.<field>")
        elif parts[0] not in {"inputs", "previous"} or len(parts) < 2:
            raise ValueError(f"invalid binding path {source!r}")
    return value, sources


def validate_bindings(bindings, nodes, *, final=False):
    for name, binding in (bindings or {}).items():
        if not name:
            raise ValueError("binding destination cannot be empty")
        _, sources = normalize_binding(binding)
        for source in sources:
            parts = _parts(source)
            if parts[0] == "nodes" and parts[1] not in nodes:
                raise ValueError(f"binding {name!r} references unknown node {parts[1]!r}")
            if final and parts[0] == "previous":
                raise ValueError("graph outputs cannot use node-local previous state")


def resolve_bindings(bindings, *, records, inputs, previous=None, partial=False):
    roots = {"inputs": inputs, "previous": previous or {},
             "nodes": {name: {"outputs": record.get("output", {})} for name, record in records.items()}}
    result = {}
    for name, binding in (bindings or {}).items():
        spec, sources = normalize_binding(binding)
        value = _MISSING
        for source in sources:
            value = roots
            for key in _parts(source):
                value = value.get(key, _MISSING) if isinstance(value, Mapping) else _MISSING
                if value is _MISSING:
                    break
            if value is not _MISSING:
                break
        if value is _MISSING:
            if "default" in spec:
                value = spec["default"]
            elif partial:
                continue
            else:
                raise ValueError(f"required input/output {name!r} missing from {sources}; declare a default for first invocation")
        result[name] = thaw(value)
    return result


def graph_outputs(graph, ctx, *, scope=None, partial=False):
    records = object.__getattribute__(ctx, "node_records").get(scope or ctx.root_scope, {})
    return resolve_bindings(graph.outputs, records=records,
                            inputs=object.__getattribute__(ctx, "values"), partial=partial)
