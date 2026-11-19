"""Task-local execution metadata; never persisted as a live Python stack."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class ExecutionFrame:
    context_id: int
    graphs: tuple[str, ...]
    path: tuple[str, ...]
    node: str | None = None
    step: int = 0
    # Scheduler activation (wave) that invoked this node. A loop target is
    # executed in a new activation, while ``step`` remains globally monotonic.
    activation: int = 0


_frame: ContextVar[ExecutionFrame | None] = ContextVar("robotenon_frame", default=None)
_writes: ContextVar[tuple] = ContextVar("robotenon_wave_writes", default=())
_claim_lock = RLock()


def current_frame(context) -> ExecutionFrame | None:
    frame = _frame.get()
    return frame if frame and frame.context_id == id(context) else None


@contextmanager
def execution_frame(frame: ExecutionFrame):
    token = _frame.set(frame)
    try:
        yield frame
    finally:
        _frame.reset(token)


@contextmanager
def wave_writes(claims):
    frame = _frame.get()
    depth = len(frame.path) if frame is not None else None
    token = _writes.set((*_writes.get(), (claims, depth)))
    try:
        yield
    finally:
        _writes.reset(token)


def claim_patch(node_id, patch):
    """Single writer per wave, including conflicting parallel nested graphs."""
    frame = _frame.get()
    scopes = [
        (claims, frame.path[depth] if frame is not None and depth is not None
         and len(frame.path) > depth else node_id)
        for claims, depth in _writes.get()
    ]
    with _claim_lock:
        # Validate all scopes before modifying any claim table.
        for claims, owner in scopes:
            for key in patch:
                if key in claims and claims[key] != owner:
                    raise ValueError(
                        f"parallel state_patch conflict for {key!r}: {claims[key]} and {owner}"
                    )
        for claims, owner in scopes:
            claims.update({key: owner for key in patch})
