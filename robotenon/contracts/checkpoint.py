"""Serializable execution checkpoint contracts.

The scheduler stores a small cursor for each graph scope.  ``RunCheckpoint``
wraps the complete JSON-safe context snapshot together with the metadata that
must be checked before a process resumes a run.  Keeping this DTO in
``contracts`` avoids making the workflow scheduler or SQLite implementation
the owner of checkpoint semantics.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TypedDict


class GraphCursor(TypedDict):
    pending: list[str]
    completed: list[str]
    activated: list[str]
    waiting: list[str]
    wave: list[str]
    done: list[str]
    executions: int
    terminal: bool
    finished: bool
    generation: int
    patch_claims: dict[str, str]
    arrivals: dict[str, dict[str, int]]
    batches: dict[str, dict]
    pending_triggers: dict[str, str]
    epochs: dict[str, int]
    loop_sources: dict[str, str]
    routed_epochs: dict[str, int]
    waiting_diagnostics: list[dict]
    routing_events: list[dict]
    routing_events_dropped: int
    loop_metrics: dict[str, dict]


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    """Versioned, JSON-safe snapshot used to resume one workflow run.

    ``state`` is deliberately opaque to the storage layer.  GraphContext
    owns its schema and provides ``to_checkpoint``/``from_checkpoint``
    adapters; a ContextStore only persists this mapping atomically.  The
    active Python execution stack is never part of the state (the context
    serializer writes an empty ``graph_stack`` and the cursor identifies the
    resumable node boundary).
    """

    checkpoint_version: int
    session_id: object | None
    graph_fingerprint: str | None
    context_revision: int
    cursors: dict[str, object]
    state: dict[str, object]

    def __post_init__(self) -> None:
        if self.checkpoint_version != 1:
            raise ValueError(
                f"unsupported checkpoint version: {self.checkpoint_version}"
            )
        if self.context_revision < 0:
            raise ValueError("checkpoint context_revision must be non-negative")
        if not isinstance(self.cursors, dict) or not isinstance(self.state, dict):
            raise TypeError("checkpoint cursors and state must be objects")

    @classmethod
    def from_context(cls, context) -> RunCheckpoint:
        """Build a defensive checkpoint from a GraphContext-like object."""

        dump = getattr(context, "to_dict", None)
        state = dump() if callable(dump) else dict(context)
        if not isinstance(state, dict):
            raise TypeError("context checkpoint serializer must return an object")
        # The cursor is duplicated as a top-level field for stores and tools
        # that need to inspect resumability without decoding the whole state.
        cursors = deepcopy(state.get("cursors") or {})
        return cls(
            checkpoint_version=int(state.get("checkpoint_version", 1)),
            session_id=deepcopy(state.get("session_id")),
            graph_fingerprint=state.get("graph_fingerprint"),
            context_revision=int(state.get("context_revision", 0)),
            cursors=cursors,
            state=deepcopy(state),
        )

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> RunCheckpoint:
        if not isinstance(value, dict):
            raise TypeError("checkpoint must be a JSON object")
        raw_state = value.get("state")
        state = deepcopy(raw_state) if isinstance(raw_state, dict) else dict(value)
        cursors = value.get("cursors")
        if not isinstance(cursors, dict):
            cursors = state.get("cursors") if isinstance(state.get("cursors"), dict) else {}
        return cls(
            checkpoint_version=int(value.get("checkpoint_version", state.get("checkpoint_version", 1))),
            session_id=deepcopy(value.get("session_id", state.get("session_id"))),
            graph_fingerprint=value.get("graph_fingerprint", state.get("graph_fingerprint")),
            context_revision=int(value.get("context_revision", state.get("context_revision", 0))),
            cursors=deepcopy(cursors),
            state=state,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a detached mapping suitable for JSON serialization."""

        return {
            "checkpoint_version": self.checkpoint_version,
            "session_id": deepcopy(self.session_id),
            "graph_fingerprint": self.graph_fingerprint,
            "context_revision": self.context_revision,
            "cursors": deepcopy(self.cursors),
            "state": deepcopy(self.state),
        }


def validate_cursor(cursor, node_ids):
    events = cursor.get("routing_events", [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ValueError("invalid checkpoint routing events")
    dropped = cursor.get("routing_events_dropped", 0)
    if type(dropped) is not int or dropped < 0:
        raise ValueError("invalid checkpoint routing event count")
    timings = cursor.get("loop_metrics", {})
    if not isinstance(timings, dict) or any(node not in node_ids or not isinstance(value, dict) for node, value in timings.items()):
        raise ValueError("invalid checkpoint loop metrics")
    for field in ("pending", "completed", "activated", "waiting", "wave", "done"):
        values = cursor.get(field)
        if not isinstance(values, list) or any(
            not isinstance(v, str) or v not in node_ids for v in values
        ):
            raise ValueError(f"invalid checkpoint cursor {field}")
        if len(values) != len(set(values)):
            raise ValueError(f"duplicate checkpoint cursor {field}")
    if not set(cursor["done"]).issubset(cursor["wave"]):
        raise ValueError("checkpoint committed nodes are outside the active wave")
    for field in ("executions", "generation"):
        if not isinstance(cursor.get(field), int) or cursor[field] < 0:
            raise ValueError(f"invalid checkpoint cursor {field}")
    for field in ("terminal", "finished"):
        if not isinstance(cursor.get(field), bool):
            raise ValueError(f"invalid checkpoint cursor {field}")
    arrivals = cursor.get("arrivals", {})
    if not isinstance(arrivals, dict):
        raise ValueError("invalid checkpoint arrivals")  # noqa: TRY004 - checkpoint validation
    for target, incoming in arrivals.items():
        if target not in node_ids or not isinstance(incoming, dict):
            raise ValueError("invalid checkpoint arrivals")
        for source, count in incoming.items():
            if source not in node_ids or type(count) is not int or count < 0:
                raise ValueError("invalid checkpoint arrival token")
            target_node = node_ids[target]
            declared = (
                target_node.has_predecessor(source)
                if hasattr(target_node, "has_predecessor")
                else source in target_node.predecessors
            )
            if hasattr(target_node, "predecessors") and not declared:
                raise ValueError("checkpoint arrival is not a declared edge")
    claims = cursor.get("patch_claims", {})
    if not isinstance(claims, dict) or any(
        not isinstance(k, str) or v not in node_ids for k, v in claims.items()
    ):
        raise ValueError("invalid checkpoint patch claims")

    batches = cursor.get("batches", {})
    triggers = cursor.get("pending_triggers", {})
    if not isinstance(batches, dict) or not isinstance(triggers, dict):
        raise ValueError("invalid checkpoint scheduling batches")
    allowed = {"entry", "next", "goto", "loop"}
    for field in ("epochs", "routed_epochs"):
        versions = cursor.get(field, {})
        if not isinstance(versions, dict) or any(
            node not in node_ids or type(version) is not int or version < 0
            for node, version in versions.items()
        ):
            raise ValueError(f"invalid checkpoint {field}")
    sources = cursor.get("loop_sources", {})
    if not isinstance(sources, dict) or any(node not in node_ids or mode not in allowed for node, mode in sources.items()):
        raise ValueError("invalid checkpoint loop condition sources")
    diagnostics = cursor.get("waiting_diagnostics", [])
    if not isinstance(diagnostics, list):
        raise ValueError("invalid checkpoint waiting diagnostics")
    for item in diagnostics:
        if not isinstance(item, dict) or item.get("node") not in node_ids:
            raise ValueError("invalid checkpoint waiting diagnostic node")
        arrived, missing = item.get("arrived"), item.get("missing")
        if not isinstance(arrived, list) or any(source not in node_ids for source in arrived):
            raise ValueError("invalid checkpoint diagnostic arrivals")
        if not isinstance(missing, dict) or any(
            source not in node_ids or reason not in {"not_completed", "not_selected", "awaiting_batch"}
            for source, reason in missing.items()
        ):
            raise ValueError("invalid checkpoint diagnostic reason")
    if any(node not in cursor["pending"] or trigger not in allowed for node, trigger in triggers.items()):
        raise ValueError("invalid checkpoint pending trigger")
    active = []
    for key, batch in batches.items():
        if not isinstance(key, str) or not key.isdecimal() or not 0 < int(key) <= cursor["generation"]:
            raise ValueError("invalid checkpoint batch generation")
        if not isinstance(batch, dict):
            raise ValueError("invalid checkpoint batch")
        nodes, modes, superseded = batch.get("nodes"), batch.get("triggers"), batch.get("superseded")
        if not isinstance(nodes, list) or not nodes or any(node not in node_ids for node in nodes):
            raise ValueError("invalid checkpoint batch nodes")
        if not isinstance(modes, dict) or set(modes) != set(nodes) or any(mode not in allowed for mode in modes.values()):
            raise ValueError("invalid checkpoint batch triggers")
        if "loop" in modes.values() and len(nodes) != 1:
            raise ValueError("self-loop must own an independent batch")
        if not isinstance(superseded, list) or len(superseded) != len(set(superseded)) or any(
            node not in nodes or (modes[node] != "loop" and not cursor["terminal"]) for node in superseded
        ):
            raise ValueError("invalid checkpoint superseded loop")
        epochs = batch.get("epochs")
        if not isinstance(epochs, dict) or set(epochs) != set(nodes) or any(
            type(version) is not int or version < 0 or version > cursor.get("epochs", {}).get(node, 0)
            for node, version in epochs.items()
        ):
            raise ValueError("invalid checkpoint batch epochs")
        for field in ("read_records", "read_fields", "read_outputs"):
            if not isinstance(batch.get(field), dict):
                raise ValueError(f"invalid checkpoint batch {field}")
        active.extend(nodes)
    if "batches" in cursor and (len(active) != len(set(active)) or set(active) != set(cursor["wave"])):
        raise ValueError("checkpoint batches disagree with active nodes")
