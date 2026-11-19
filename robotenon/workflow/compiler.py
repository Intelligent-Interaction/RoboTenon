"""Validate graph references and normalize legacy edges once at assembly."""

import hashlib
import json
from dataclasses import dataclass

from .definition import GraphDefinition, GraphGoto, GraphsConfig, _validate_graphs


@dataclass(frozen=True)
class CompiledGraph:
    definition: GraphDefinition
    successors: dict[str, tuple[str, ...]]


def compile_graphs(graphs, modules, tools=None):
    definitions = list(graphs.values())
    _validate_graphs(GraphsConfig(graphs=definitions))
    compiled = {}
    for original in definitions:
        graph = original.model_copy(deep=True)
        if graph.edges and graph.uses_predecessors():
            raise ValueError(
                f"graph {graph.id!r} cannot mix edges with predecessors/next"
            )
        if not graph.uses_predecessors():
            nodes = graph.node_map()
            for node in graph.nodes:
                rules = [GraphGoto(node=edge.target, when=edge.condition)
                         for edge in graph.outgoing(node.id)]
                # Preserve the conventional trailing fallback spelling.
                if rules and any(rule.when for rule in rules) and rules[-1].when is None:
                    node.default = rules.pop().node
                node.next = rules
                for target in [rule.node for rule in rules] + [node.default]:
                    if target in nodes and not nodes[target].has_predecessor(node.id):
                        nodes[target].predecessors.append(node.id)
            graph.edges = []
        successors = {node.id: [] for node in graph.nodes}
        for node in graph.nodes:
            if node.module:
                module = modules.resolve(node.module)
                if not callable(getattr(module, "run", None)):
                    raise TypeError(
                        f"graph module {node.module!r} must implement run(ctx)"
                    )
            for predecessor in node.flat_predecessors():
                successors[predecessor].append(node.id)
        # Next dependencies are an AND DAG; goto and self-loop routes may
        # invoke a node without new predecessor tokens.
        indegree = {node.id: len(node.flat_predecessors()) for node in graph.nodes}
        ready = [name for name, count in indegree.items() if count == 0]
        visited = 0
        while ready:
            name = ready.pop()
            visited += 1
            for target in successors[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(graph.nodes):
            raise ValueError(f"graph {graph.id!r} predecessor cycle; use goto for back jumps")
        # Round invalidation follows both dependency and ordinary control edges.
        for node in graph.nodes:
            for target in [rule.node for rule in node.next] + [node.default]:
                if target in successors and target not in successors[node.id]:
                    successors[node.id].append(target)
        indegree = dict.fromkeys(successors, 0)
        for targets in successors.values():
            for target in targets:
                indegree[target] += 1
        ready = [name for name, count in indegree.items() if count == 0]
        visited = 0
        while ready:
            name = ready.pop()
            visited += 1
            for target in successors[name]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    ready.append(target)
        if visited != len(graph.nodes):
            raise ValueError(f"graph {graph.id!r} next/dependency cycle; use goto or loop for repetition")
        for entry in graph.entry_nodes():
            if graph.node_map()[entry].predecessors:
                raise ValueError(f"entry {entry!r} cannot have predecessors; use goto for re-entry")
        compiled[graph.id] = CompiledGraph(
            graph, {k: tuple(v) for k, v in successors.items()}
        )
    payload = {
        "workspace_version": 1,
        "routing_version": 6,
        "graphs": [
            compiled[key].definition.model_dump(mode="json") for key in sorted(compiled)
        ],
        "modules": [
            {k: v for k, v in item.items() if k != "started"}
            for item in modules.describe()
        ],
    }
    if tools is not None:
        payload["tools"] = [
            {k: v for k, v in item.items() if k != "started"}
            for item in tools.describe()
        ]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return compiled, fingerprint
