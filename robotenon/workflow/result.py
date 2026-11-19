import json
from copy import deepcopy
from dataclasses import dataclass, field

from ..contracts.context import GraphContext, GraphStatus
from ..contracts.serialization import jsonable


@dataclass(slots=True)
class GraphResult:
    graph_id: str
    status: GraphStatus
    success: bool
    steps: int
    context: GraphContext
    error: str | None = None
    outputs: dict = field(default_factory=dict)

    @property
    def routing_events(self) -> list[dict]:
        """Bounded recent decisions/timing; full export uses the Trace client."""
        return [jsonable(event) for scope, cursor in self.context.cursors.items()
                if json.loads(scope)[-1] == self.graph_id
                for event in cursor.get("routing_events", [])]

    @property
    def routing_events_dropped(self) -> int:
        return sum(cursor.get("routing_events_dropped", 0) for scope, cursor in self.context.cursors.items()
                   if json.loads(scope)[-1] == self.graph_id)

    @property
    def loop_metrics(self) -> list[dict]:
        return [dict(jsonable(stats), node=node, graph=self.graph_id, scope=json.loads(scope))
                for scope, cursor in self.context.cursors.items() if json.loads(scope)[-1] == self.graph_id
                for node, stats in cursor.get("loop_metrics", {}).items()]

    @property
    def routing_diagnostics(self) -> list[dict]:
        """Unreleased AND joins, grouped by this graph's invocation scope."""
        return [dict(deepcopy(item), graph=self.graph_id, scope=json.loads(scope))
                for scope, cursor in self.context.cursors.items()
                if json.loads(scope)[-1] == self.graph_id
                for item in cursor.get("waiting_diagnostics", [])]


@dataclass(frozen=True, slots=True)
class GraphTransitionRequest:
    """Internal control result that replaces the active root graph."""

    graph_id: str
    inputs: dict = field(default_factory=dict)
    status: str = "transition"
    success: bool = True
