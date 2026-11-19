"""Declarative graph definitions loaded from one JSON document."""

from __future__ import annotations

import math
import re
import warnings
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from ..schemas import ConfigRoboModel, SafetyLevel, field_validator
from .conditions import parse_condition, validate_condition_references

END = "__end__"
SYSTEM_OPERATIONS = frozenset(
    {
        "graph.stop",
        "env.reset",
        "env.observe",
        "env.close",
        "body.execute",
        "body.interrupt",
        "monitor.check",
        "monitor.latest",
        "monitor.request",
        "safety.snapshot",
    }
)


class GraphGoto(ConfigRoboModel):
    """Conditional target, shared by the independent next and goto fields."""

    node: str
    when: str | None = None


class GraphTransition(ConfigRoboModel):
    """Replace the active graph with another graph without nesting it."""

    graph: str
    when: str | None = None
    inputs: dict[str, Any] | None = None


class SafetyRecovery(ConfigRoboModel):
    """Recovery policy selected after an emergency level is reached.

    ``mode=auto`` permits a trusted supervisor to recover transient P1/P2
    conditions without a human acknowledgement.  ``mode=manual`` keeps the
    level latched until the caller passes ``approved=True`` to
    :meth:`GraphRuntime.recover`.  The optional graphs run with the same
    global context and environment service as the interrupted graph.
    """

    mode: Literal["auto", "manual"] = "manual"
    recovery_graph: str | None = None
    resume_graph: str | None = None
    clear_latched: bool = False


_ROUTE_PATTERN = re.compile(
    r"^\s*(?:(?P<operation>next|goto|loop)\s+)?(?P<node>[^\s]+?)(?:\s+when\s+(?P<when>.+))?\s*$",
    re.IGNORECASE,
)


def _normalize_route(raw: Any, operation: str) -> dict[str, Any]:
    if isinstance(raw, GraphGoto):
        raw = raw.model_dump()
    if isinstance(raw, str):
        match = _ROUTE_PATTERN.match(raw)
        if not match:
            raise ValueError(f"invalid {operation} statement: {raw!r}")
        prefix = match.group("operation")
        if prefix and prefix.lower() != operation:
            raise ValueError(f"use the {prefix.lower()} field for {raw!r}")
        return {"node": match.group("node"), "when": match.group("when")}
    if not isinstance(raw, Mapping):
        raise TypeError(f"{operation} rules must be strings or objects")
    value = dict(raw)
    if "kind" in value or "kinde" in value:
        raise ValueError("route kind is no longer supported; use separate next/goto/loop fields")
    if "node" not in value:
        for alias in ("to", "target"):
            if alias in value:
                value["node"] = value.pop(alias)
                break
    if "when" not in value and "condition" in value:
        value["when"] = value.pop("condition")
    if not value.get("node"):
        raise ValueError(f"{operation} rule must set node/to/target")
    if set(value) - {"node", "when"}:
        raise ValueError(f"unknown {operation} rule fields: {sorted(set(value) - {'node', 'when'})}")
    return value


def _normalize_transition(raw: Any) -> dict[str, Any]:
    if isinstance(raw, GraphTransition):
        raw = raw.model_dump()
    if isinstance(raw, str):
        return {"graph": raw}
    if not isinstance(raw, Mapping):
        raise TypeError("transition rules must be graph names or objects")
    value = dict(raw)
    if "graph" not in value:
        for alias in ("to", "target"):
            if alias in value:
                value["graph"] = value.pop(alias)
                break
    if "when" not in value and "condition" in value:
        value["when"] = value.pop("condition")
    if not value.get("graph"):
        raise ValueError("transition rule must set graph/to/target")
    allowed = {"graph", "when", "inputs"}
    if set(value) - allowed:
        raise ValueError(
            f"unknown transition rule fields: {sorted(set(value) - allowed)}"
        )
    return value


class GraphLoop(ConfigRoboModel):
    """A self-repeat condition. The enclosing node is the only target."""

    when: str | None = None
    every: str | None = None

    @field_validator("every", mode="before")
    @classmethod
    def _validate_every(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)(?:ms|s)", value):
            raise ValueError("loop.every must be a positive duration with units, e.g. 20ms or 0.5s")
        seconds = float(value[:-2]) / 1000 if value.endswith("ms") else float(value[:-1])
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("loop.every must be finite and positive")
        return value

    def interval_seconds(self):
        if self.every is None:
            return 0.0
        return float(self.every[:-2]) / 1000 if self.every.endswith("ms") else float(self.every[:-1])


class GraphNode(ConfigRoboModel):
    """A module, nested graph or runtime system operation.

    ``type`` is optional for concise manifests.  When omitted it is inferred
    from ``module``, ``graph`` or ``operation``.  Explicit ``type`` keeps a
    manifest self-documenting and is required for system nodes in examples.

    ``predecessors`` is the preferred way to describe graph topology. Strings
    at the outer level are AND dependencies; one inner list is an OR group.
    Thus ``["a", ["b", "c"]]`` means ``a AND (b OR c)``. A node becomes
    eligible only after every outer group receives a selected predecessor.
    ``next`` releases predecessor tokens; ``goto`` directly invokes a node;
    ``loop`` repeats this node independently until a normal activation replaces
    that loop. ``transition`` ends the current graph and starts another graph
    without nesting it. All matching rules fire; ``default`` is used only if
    no node route or graph transition matches.
    """

    id: str
    type: Literal["module", "graph", "system"] | None = None
    module: str | None = None
    graph: str | None = None
    operation: str | None = None
    inputs: dict[str, Any] | None = None
    predecessors: list[str | list[str]] = Field(default_factory=list)
    next: list[GraphGoto] = Field(default_factory=list)
    goto: list[GraphGoto] = Field(default_factory=list)
    transition: list[GraphTransition] = Field(default_factory=list)
    default: str | None = None
    loop: GraphLoop | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("predecessors", mode="before")
    @classmethod
    def _normalize_predecessors(cls, value: Any) -> list[str | list[str]]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise TypeError("predecessors must be a list of node ids or OR groups")
        normalized: list[str | list[str]] = []
        seen: set[str] = set()
        for item in value:
            if isinstance(item, str):
                group = [item]
                stored: str | list[str] = item
            elif isinstance(item, (list, tuple)):
                if not item:
                    raise ValueError("predecessor OR groups cannot be empty")
                if any(not isinstance(source, str) for source in item):
                    raise ValueError(
                        "predecessor OR groups support exactly two levels"
                    )
                group = list(item)
                stored = group
            else:
                raise TypeError("predecessors must contain node ids or OR groups")
            if any(not source for source in group):
                raise ValueError("predecessor node ids cannot be empty")
            duplicates = seen.intersection(group)
            repeated_in_group = {x for x in group if group.count(x) > 1}
            if duplicates or repeated_in_group:
                repeated = sorted(duplicates | repeated_in_group)
                raise ValueError(
                    "predecessors cannot repeat a node across groups: "
                    + ", ".join(repeated)
                )
            seen.update(group)
            normalized.append(stored)
        return normalized

    def predecessor_groups(self) -> tuple[tuple[str, ...], ...]:
        """Return normalized outer-AND/inner-OR dependency groups."""

        return tuple(
            (item,) if isinstance(item, str) else tuple(item)
            for item in self.predecessors
        )

    def flat_predecessors(self) -> tuple[str, ...]:
        """Return every dependency edge, ignoring grouping."""

        return tuple(
            source for group in self.predecessor_groups() for source in group
        )

    def has_predecessor(self, source: str) -> bool:
        return source in self.flat_predecessors()

    @field_validator("next", mode="before")
    @classmethod
    def _normalize_next(cls, value: Any):
        if value is None:
            return []
        if isinstance(value, (str, Mapping, GraphGoto)):
            value = [value]
        return [_normalize_route(item, "next") for item in value]

    @field_validator("goto", mode="before")
    @classmethod
    def _normalize_goto(cls, value: Any):
        if value is None:
            return []
        if isinstance(value, (str, Mapping, GraphGoto)):
            value = [value]
        return [_normalize_route(item, "goto") for item in value]

    @field_validator("transition", mode="before")
    @classmethod
    def _normalize_transitions(cls, value: Any):
        if value is None:
            return []
        if isinstance(value, (str, Mapping, GraphTransition)):
            value = [value]
        return [_normalize_transition(item) for item in value]

    @field_validator("loop", mode="before")
    @classmethod
    def _normalize_loop(cls, value: Any):
        if value is None or value is False:
            return None
        if value is True:
            return {}
        if isinstance(value, GraphLoop):
            value = value.model_dump()
        if not isinstance(value, Mapping) or set(value) - {"when", "every"}:
            raise ValueError("loop only repeats this node; use true or {when: condition, every: duration}, and goto for another node")
        return dict(value)

    def routes(self) -> list[tuple[str, GraphGoto]]:
        routes = [("next", rule) for rule in self.next] + [("goto", rule) for rule in self.goto]
        if self.loop is not None:
            routes.append(("loop", GraphGoto(node=self.id, when=self.loop.when)))
        return routes

    def resolved_type(self) -> Literal["module", "graph", "system"]:
        if self.type is not None:
            return self.type
        if self.operation is not None:
            return "system"
        if self.graph is not None:
            return "graph"
        return "module"


class GraphEdge(ConfigRoboModel):
    """Legacy ordinary dependency edge, compiled to next rules."""

    source: str
    target: str
    condition: str | None = None


class GraphDefinition(ConfigRoboModel):
    """One graph with an entry node and declarative node dependencies.

    ``edges`` remains accepted as a compatibility format for manifests written
    before predecessor/next syntax was introduced.  New graphs should omit it
    and put ``predecessors`` and ``next`` directly on each node.
    """

    id: str
    entry: str | list[str]
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    emergency_graphs: dict[str, str] = Field(default_factory=dict)
    recovery: dict[str, SafetyRecovery] = Field(default_factory=dict)
    max_steps: int = Field(default=1000, ge=1)
    backend: str = "langgraph"

    @field_validator("recovery", mode="before")
    @classmethod
    def _normalize_recovery(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("recovery must be an object keyed by SafetyLevel")
        normalized: dict[str, Any] = {}
        for level, policy in value.items():
            if isinstance(policy, str):
                normalized[str(level)] = {"recovery_graph": policy}
            elif isinstance(policy, Mapping):
                item = dict(policy)
                if item.get("mode") == "automatic":
                    item["mode"] = "auto"
                normalized[str(level)] = item
            else:
                raise TypeError("recovery policies must be strings or objects")
        return normalized

    def node_map(self) -> dict[str, GraphNode]:
        return {node.id: node for node in self.nodes}

    def outgoing(self, source: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.source == source]

    def entry_nodes(self) -> list[str]:
        """Return one or more initial nodes, preserving manifest order."""
        return [self.entry] if isinstance(self.entry, str) else list(self.entry)

    def uses_predecessors(self) -> bool:
        """Whether this graph uses the parallel predecessor/next format."""
        return any(
            node.predecessors
            or node.next
            or node.goto
            or node.transition
            or node.loop
            or node.default is not None
            for node in self.nodes
        )

    def emergency_graph_for(self, level: SafetyLevel | str) -> str | None:
        """Select the most specific configured emergency graph for a level."""
        normalized = level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
        matches: list[tuple[int, str]] = []
        for raw_level, graph_id in self.emergency_graphs.items():
            try:
                candidate = SafetyLevel(str(raw_level).upper())
            except ValueError:
                continue
            if candidate.rank <= normalized.rank:
                matches.append((candidate.rank, graph_id))
        return max(matches, default=(0, None))[1]

    def recovery_for(self, level: SafetyLevel | str) -> SafetyRecovery | None:
        """Select the most specific recovery policy for a current level."""
        normalized = level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
        matches: list[tuple[int, SafetyRecovery]] = []
        for raw_level, policy in self.recovery.items():
            try:
                candidate = SafetyLevel(str(raw_level).upper())
            except ValueError:
                continue
            if candidate.rank <= normalized.rank:
                matches.append((candidate.rank, policy))
        return max(matches, key=lambda item: item[0], default=(0, None))[1]


class GraphsConfig(ConfigRoboModel):
    """A single JSON file may contain any number of named graphs."""

    config_version: int = Field(default=1, ge=1)
    graphs: list[GraphDefinition] = Field(default_factory=list)

    def graph_map(self) -> dict[str, GraphDefinition]:
        return {graph.id: graph for graph in self.graphs}


def _normalize_edge(raw: dict[str, Any]) -> dict[str, Any]:
    # JSON uses the readable ``from``/``to`` spelling; Python uses source and
    # target so callers never need a reserved-word field name.
    value = dict(raw)
    if "kind" in value or "kinde" in value:
        raise ValueError("edge kind is no longer supported; use node next/goto/loop fields")
    if "source" not in value and "from" in value:
        value["source"] = value.pop("from")
    if "target" not in value and "to" in value:
        value["target"] = value.pop("to")
    if "when" in value and "condition" not in value:
        value["condition"] = value.pop("when")
    return value


def _normalize_node(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize node aliases used by early graph manifests."""
    value = dict(raw)
    if "predecessors" not in value:
        if "predecessor" in value:
            value["predecessors"] = value.pop("predecessor")
        elif "depends_on" in value:
            value["predecessors"] = value.pop("depends_on")
    return value


def load_graphs_config(source: Any) -> GraphsConfig:
    """Load, normalize and validate graphs from a manifest or Params bundle."""
    from ..infra.params import config_manifest

    value = config_manifest(source, "graphs")
    if not isinstance(value, dict):
        raise ValueError("graph config root must be an object")
    if "graphs" not in value or not isinstance(value["graphs"], list):
        raise ValueError("graph config must contain a 'graphs' array")
    raw_graphs: list[dict[str, Any]] = []
    for raw in value["graphs"]:
        if not isinstance(raw, dict):
            raise ValueError("each graph must be an object")
        graph = dict(raw)
        if "recovery" not in graph and "safety_recovery" in graph:
            graph["recovery"] = graph.pop("safety_recovery")
        if "recovery" not in graph and "recovery_graphs" in graph:
            graph["recovery"] = graph.pop("recovery_graphs")
        graph["nodes"] = [_normalize_node(node) for node in graph.get("nodes", [])]
        graph["edges"] = [_normalize_edge(edge) for edge in graph.get("edges", [])]
        if isinstance(graph.get("entry"), str):
            graph["entry"] = graph["entry"]
        elif graph.get("entry") is not None:
            graph["entry"] = list(graph["entry"])
        raw_graphs.append(graph)
    config = GraphsConfig.model_validate({**value, "graphs": raw_graphs})
    _validate_graphs(config)
    return config


def _validate_graphs(config: GraphsConfig) -> None:
    graph_map = config.graph_map()
    if len(graph_map) != len(config.graphs):
        raise ValueError("graph ids must be unique")
    for graph in config.graphs:
        if graph.backend.lower() not in {"langgraph", "fallback"}:
            raise ValueError(
                f"graph {graph.id!r} has unsupported backend {graph.backend!r}"
            )
        for raw_level, emergency_graph in graph.emergency_graphs.items():
            try:
                SafetyLevel(str(raw_level).upper())
            except ValueError as exc:
                raise ValueError(
                    f"graph {graph.id!r} has invalid emergency safety level {raw_level!r}"
                ) from exc
            if emergency_graph not in graph_map:
                raise ValueError(
                    f"graph {graph.id!r} emergency graph {emergency_graph!r} is unknown"
                )
        for raw_level, policy in graph.recovery.items():
            try:
                SafetyLevel(str(raw_level).upper())
            except ValueError as exc:
                raise ValueError(f"graph {graph.id!r} has invalid recovery safety level {raw_level!r}") from exc
            for field_name in ("recovery_graph", "resume_graph"):
                target = getattr(policy, field_name)
                if target is not None and target not in graph_map:
                    raise ValueError(
                        f"graph {graph.id!r} recovery {field_name} {target!r} is unknown"
                    )
        nodes = graph.node_map()
        from .bindings import validate_bindings
        validate_bindings(graph.outputs, nodes, final=True)
        for declared_node in graph.nodes:
            validate_bindings(declared_node.inputs, nodes)
            for transition in declared_node.transition:
                validate_bindings(transition.inputs, nodes)
        if len(nodes) != len(graph.nodes):
            raise ValueError(f"graph {graph.id!r} contains duplicate node ids")
        entries = graph.entry_nodes()
        if not entries:
            raise ValueError(f"graph {graph.id!r} must declare at least one entry node")
        for entry in entries:
            if entry not in nodes:
                raise ValueError(f"graph {graph.id!r} entry {entry!r} is not a node")
        for node in graph.nodes:
            if node.operation == "graph.stop" and (
                node.routes() or node.transition or node.default is not None
            ):
                raise ValueError("graph.stop cannot have outgoing routes")
            if node.default is not None and any(
                rule.when is None or rule.when.strip().lower() in {"", "true"}
                for rule in [*[item for _, item in node.routes()], *node.transition]
            ):
                warnings.warn(
                    f"node {graph.id}.{node.id}: default is unreachable because a route is unconditional",
                    UserWarning, stacklevel=2,
                )
            node_type = node.resolved_type()
            if node_type == "module":
                if not node.module or node.graph is not None or node.operation is not None:
                    raise ValueError(
                        f"node {graph.id}.{node.id} of type module must set only module"
                    )
            elif node_type == "graph":
                if not node.graph or node.module is not None or node.operation is not None:
                    raise ValueError(
                        f"node {graph.id}.{node.id} of type graph must set only graph"
                    )
            elif node_type == "system":
                if not node.operation or node.module is not None or node.graph is not None:
                    raise ValueError(
                        f"node {graph.id}.{node.id} of type system must set only operation"
                    )
                dynamic_monitor_request = (
                    node.operation.startswith("monitor.request.")
                    and bool(node.operation.rsplit(".", 1)[-1])
                )
                if node.operation not in SYSTEM_OPERATIONS and not dynamic_monitor_request:
                    allowed = ", ".join(sorted(SYSTEM_OPERATIONS))
                    raise ValueError(
                        f"node {graph.id}.{node.id} has unsupported system operation "
                        f"{node.operation!r}; allowed: {allowed}"
                    )
            else:  # pragma: no cover - Pydantic validates the Literal first
                raise ValueError(f"node {graph.id}.{node.id} has unknown type {node_type!r}")
            if node_type == "graph" and node.graph not in graph_map:
                raise ValueError(
                    f"node {graph.id}.{node.id} references unknown graph {node.graph!r}"
                )
            for predecessor in node.flat_predecessors():
                if predecessor not in nodes:
                    raise ValueError(
                        f"graph {graph.id!r} node {node.id!r} predecessor {predecessor!r} is unknown"
                    )
            targets = [rule.node for rule in node.next]
            if node.default is not None:
                targets.append(node.default)
            for target in targets:
                if target.lower() in {END, "end"}:
                    continue
                if target not in nodes:
                    raise ValueError(f"graph {graph.id!r} node {node.id!r} goto target {target!r} is unknown")
                if not nodes[target].has_predecessor(node.id):
                    raise ValueError(f"edge {node.id}->{target} requires target predecessors to include {node.id!r}")
            for predecessor in node.flat_predecessors():
                source = nodes[predecessor]
                if node.id not in [rule.node for rule in source.next] + [source.default]:
                    raise ValueError(f"edge {predecessor}->{node.id} requires source next or default")
            for operation, goto in node.routes():
                if goto.node not in nodes and goto.node.lower() not in {END, "end"}:
                    raise ValueError(
                        f"graph {graph.id!r} node {node.id!r} goto target {goto.node!r} is unknown"
                    )
                if goto.when is not None and not isinstance(goto.when, str):
                    raise ValueError(f"graph {graph.id!r} node {node.id!r} goto when must be a string or null")
                if goto.when and goto.when.strip():
                    parse_condition(goto.when)
                    validate_condition_references(goto.when, nodes, f"node {graph.id}.{node.id} {operation}")
            for transition in node.transition:
                if transition.graph not in graph_map:
                    raise ValueError(
                        f"graph {graph.id!r} node {node.id!r} transition target "
                        f"{transition.graph!r} is unknown"
                    )
                if transition.when is not None and not isinstance(transition.when, str):
                    raise ValueError(
                        f"graph {graph.id!r} node {node.id!r} transition when "
                        "must be a string or null"
                    )
                if transition.when and transition.when.strip():
                    parse_condition(transition.when)
                    validate_condition_references(
                        transition.when,
                        nodes,
                        f"node {graph.id}.{node.id} transition",
                    )
        for edge in graph.edges:
            if "kind" in edge.model_dump() or "kinde" in edge.model_dump():
                raise ValueError("edge kind is no longer supported; use node next/goto/loop fields")
            if edge.source not in nodes:
                raise ValueError(f"graph {graph.id!r} edge source {edge.source!r} is unknown")
            if edge.target not in nodes and edge.target.lower() not in {END, "end"}:
                raise ValueError(f"graph {graph.id!r} edge target {edge.target!r} is unknown")
            if edge.condition is not None and not isinstance(edge.condition, str):
                raise ValueError(f"graph {graph.id!r} edge condition must be a string or null")
            if edge.condition and edge.condition.strip():
                parse_condition(edge.condition)
                validate_condition_references(edge.condition, nodes, f"edge {graph.id}.{edge.source}->{edge.target}")


__all__ = [
    "END",
    "SYSTEM_OPERATIONS",
    "GraphDefinition",
    "GraphEdge",
    "GraphGoto",
    "GraphLoop",
    "GraphNode",
    "GraphsConfig",
    "SafetyRecovery",
    "load_graphs_config",
]
