"""Process-wide P0-P4 safety state and fail-safe action gating."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from ..schemas import (
    Action,
    ActionType,
    EnvironmentFeedback,
    SafetyLevel,
    SafetySignal,
    SafetyStateSnapshot,
)


def safety_rank(level: SafetyLevel | str) -> int:
    normalized = level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
    return normalized.rank


def max_safety_level(*levels: SafetyLevel | str) -> SafetyLevel:
    normalized = [level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper()) for level in levels]
    return max(normalized, key=lambda item: item.rank, default=SafetyLevel.P0)


def safety_mode(level: SafetyLevel | str) -> str:
    normalized = level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
    return {
        SafetyLevel.P0: "normal",
        SafetyLevel.P1: "monitor",
        SafetyLevel.P2: "limited",
        SafetyLevel.P3: "pause",
        SafetyLevel.P4: "emergency_stop",
    }[normalized]


def merge_signal(previous: SafetySignal | None, incoming: SafetySignal) -> SafetySignal:
    """Updates may escalate an unacknowledged incident, never lower its latch."""
    if previous is not None and previous.latched and previous.level.rank >= incoming.level.rank:
        return previous
    latched = incoming.latched or incoming.level.rank >= SafetyLevel.P3.rank or bool(previous and previous.latched)
    return incoming.model_copy(update={"latched": latched})


def reconcile_safety_snapshot(
    store: Any,
    owner: str,
    level: SafetyLevel | str,
    signals: Iterable[SafetySignal] = (),
    *,
    source: str | None = None,
    reason: str | None = None,
    synthetic_code: str | None = None,
) -> SafetyStateSnapshot:
    """Publish one producer's complete safety snapshot.

    Monitors and evaluators emit snapshots, not append-only logs.  Keeping this
    policy in one helper makes the in-process and SQLite stores behave the same:
    stale transient signals owned by the producer disappear, while latched
    P3/P4 signals remain until explicit recovery.  A summary level is materialized
    as a synthetic signal when the detailed signal list does not already carry
    that severity, so ``report.safety_level`` can never be lost.
    """

    owner = str(owner).strip()
    if not owner:
        raise ValueError("safety signal owner must be non-empty")
    normalized = (
        level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
    )
    incoming = list(signals)
    if normalized.rank > SafetyLevel.P0.rank and not any(
        item.level.rank >= normalized.rank for item in incoming
    ):
        incoming.append(
            SafetySignal(
                code=synthetic_code or f"{owner}.level",
                level=normalized,
                source=source or owner,
                message=reason,
                latched=normalized.rank >= SafetyLevel.P3.rank,
            )
        )
    reconcile = getattr(store, "reconcile", None)
    if callable(reconcile):
        return reconcile(owner, incoming, replace_transient=True)

    # Compatibility for third-party stores predating owner-aware reconcile.
    # Only clear the two reserved summary codes; never globally clear another
    # producer's transient or latched signal.
    for code in (synthetic_code or f"{owner}.level", f"{owner}.compliance"):
        clear = getattr(store, "clear", None)
        if callable(clear):
            clear(code=code, include_latched=False)
    if incoming:
        update = getattr(store, "update", None)
        if callable(update):
            return update(incoming, replace_transient=False)
    snapshot = getattr(store, "snapshot", None)
    if not callable(snapshot):
        raise TypeError("safety store must implement snapshot()")
    return snapshot()


async def async_safety_call(
    store: Any,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a safety-store method without blocking an asyncio loop.

    The public safety-store contract is intentionally synchronous so Body and
    hardware watchdogs can make a fail-safe decision without depending on an
    event loop.  The SQLite implementation therefore performs blocking WAL
    work in its normal methods.  Workflow, Monitor and Environment are async
    services, though, and must isolate those calls from their control loops.
    In-memory stores advertise ``non_blocking=True``; all other synchronous
    stores are dispatched to a worker thread.  A third-party store may expose
    an async method and is awaited directly.
    """

    method = getattr(store, method_name, None)
    if not callable(method):
        raise AttributeError(f"safety store does not implement {method_name}()")
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    if getattr(store, "non_blocking", False):
        return method(*args, **kwargs)
    result = await asyncio.to_thread(method, *args, **kwargs)
    # A callable object may return an awaitable even when inspect cannot see
    # its async ``__call__``; support that shape without a second thread hop.
    if inspect.isawaitable(result):
        return await result
    return result


async def async_reconcile_safety_snapshot(
    store: Any,
    owner: str,
    level: SafetyLevel | str,
    signals: Iterable[SafetySignal] = (),
    *,
    source: str | None = None,
    reason: str | None = None,
    synthetic_code: str | None = None,
) -> SafetyStateSnapshot:
    """Async counterpart of :func:`reconcile_safety_snapshot`.

    ``reconcile_safety_snapshot`` is intentionally synchronous for hardware
    watchdogs.  This adapter keeps the exact same merge/latch semantics while
    dispatching a SQLite-backed store away from an async monitor/workflow.
    """

    kwargs = {
        "source": source,
        "reason": reason,
        "synthetic_code": synthetic_code,
    }
    if getattr(store, "non_blocking", False):
        return reconcile_safety_snapshot(store, owner, level, signals, **kwargs)
    result = await asyncio.to_thread(
        reconcile_safety_snapshot,
        store,
        owner,
        level,
        tuple(signals),
        **kwargs,
    )
    return result


class SafetyStateStore:
    """Thread-safe process-wide safety signal store."""

    # Memory operations only hold a short RLock and never touch the disk.
    # Async callers can execute them inline; SQLiteSafetyStateStore overrides
    # this marker because its inherited API is backed by WAL transactions.
    non_blocking = True

    def __init__(self) -> None:
        self._lock = RLock()
        self._signals: dict[str, SafetySignal] = {}
        self._heartbeats: dict[str, tuple[datetime, SafetyLevel]] = {}
        self._expired_heartbeats: set[str] = set()
        self._revision = 0
        self._updated_at = datetime.now(timezone.utc)
        self._incident_id: UUID | None = None
        self._incident_started_at: datetime | None = None

    def _snapshot_unlocked(self) -> SafetyStateSnapshot:
        now = datetime.now(timezone.utc)
        # Expired non-latched producer signals are removed lazily on read. A
        # latched P3/P4 signal is never removed by a TTL.
        expired = [
            code for code, signal in self._signals.items()
            if signal.expires_at is not None and signal.expires_at <= now and not signal.latched
        ]
        for code in expired:
            self._signals.pop(code, None)
        active = list(self._signals.values())
        expired_heartbeats: set[str] = set()
        for source, (expires_at, level) in self._heartbeats.items():
            if expires_at <= now:
                expired_heartbeats.add(source)
                active.append(
                    SafetySignal(
                        code=f"{source}.heartbeat",
                        level=level,
                        source=source,
                        message="service heartbeat expired",
                        timestamp=now,
                        expires_at=expires_at,
                        latched=False,
                    )
                )
        newly_expired = expired_heartbeats - self._expired_heartbeats
        if newly_expired:
            self._expired_heartbeats.update(newly_expired)
            self._revision += 1
            self._updated_at = now
        level = max((signal.level for signal in active), key=lambda item: item.rank, default=SafetyLevel.P0)
        if level.rank > SafetyLevel.P0.rank and self._incident_id is None:
            self._incident_id = uuid4()
            self._incident_started_at = now
            self._revision += 1
            self._updated_at = now
        elif level is SafetyLevel.P0:
            if self._incident_id is not None:
                self._incident_id = None
                self._incident_started_at = None
                self._revision += 1
                self._updated_at = now
        return SafetyStateSnapshot(
            level=level,
            mode=safety_mode(level),
            signals=active,
            revision=self._revision,
            updated_at=self._updated_at,
            incident_id=self._incident_id,
            incident_started_at=self._incident_started_at,
        )

    def snapshot(self) -> SafetyStateSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def update(self, signals: Iterable[SafetySignal] = (), *, level: SafetyLevel | str | None = None,
               source: str = "safety", replace_transient: bool = True) -> SafetyStateSnapshot:
        incoming = list(signals)
        if level is not None:
            normalized = level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
            if not any(signal.level == normalized for signal in incoming):
                incoming.append(SafetySignal(code=f"{source}.level", level=normalized, source=source,
                                             latched=normalized.rank >= SafetyLevel.P3.rank))
        with self._lock:
            if replace_transient:
                self._signals = {code: signal for code, signal in self._signals.items() if signal.latched}
            for signal in incoming:
                self._signals[signal.code] = merge_signal(self._signals.get(signal.code), signal)
            self._revision += 1
            self._updated_at = datetime.now(timezone.utc)
            return self._snapshot_unlocked()

    def reconcile(
        self,
        owner: str,
        signals: Iterable[SafetySignal] = (),
        *,
        replace_transient: bool = True,
        clear_latched: bool = False,
    ) -> SafetyStateSnapshot:
        """Publish the complete current signal set owned by ``owner``.

        Monitor reports are snapshots, not append-only events.  The old
        implementation appended every custom signal and only cleared the
        synthetic ``<monitor>.compliance`` code, so recovered transient
        signals could remain active forever.  This method tags incoming
        signals with an owner and removes stale *transient* signals from that
        owner.  Latched P3/P4 signals remain until an explicit recovery call
        (``clear_latched=True``), preserving fail-safe behaviour.
        """

        owner = str(owner).strip()
        if not owner:
            raise ValueError("safety signal owner must be non-empty")
        incoming: list[SafetySignal] = []
        for signal in signals:
            metadata = dict(signal.metadata or {})
            metadata.setdefault("_owner", owner)
            incoming.append(signal.model_copy(update={"metadata": metadata}))
        incoming_codes = {signal.code for signal in incoming}

        def owned(code: str, signal: SafetySignal) -> bool:
            metadata = signal.metadata or {}
            return (
                metadata.get("_owner") == owner
                or signal.source == owner
                or code == f"{owner}.compliance"
                or code.startswith(f"{owner}.")
            )

        with self._lock:
            changed = False
            if replace_transient:
                for code, previous in list(self._signals.items()):
                    if (
                        owned(code, previous)
                        and code not in incoming_codes
                        and (clear_latched or not previous.latched)
                    ):
                        self._signals.pop(code, None)
                        changed = True
            for signal in incoming:
                merged = merge_signal(self._signals.get(signal.code), signal)
                if self._signals.get(signal.code) != merged:
                    changed = True
                self._signals[signal.code] = merged
            if changed or incoming:
                self._revision += 1
                self._updated_at = datetime.now(timezone.utc)
            return self._snapshot_unlocked()

    def raise_level(self, level: SafetyLevel | str, *, code: str, source: str = "safety",
                    message: str | None = None, latched: bool | None = None,
                    metadata: dict[str, Any] | None = None) -> SafetyStateSnapshot:
        normalized = level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
        signal = SafetySignal(code=code, level=normalized, source=source, message=message,
                              latched=normalized.rank >= SafetyLevel.P3.rank if latched is None else latched,
                              metadata=metadata or {})
        return self.update([signal], replace_transient=False)

    def observe_feedback(self, feedback: EnvironmentFeedback) -> SafetyStateSnapshot:
        signals: list[SafetySignal] = []
        for item in feedback.signals:
            level = item.safety_level
            if level is None and isinstance(item.value, dict):
                raw = item.value.get("safety_level", item.value.get("level"))
                if raw is not None:
                    try:
                        level = SafetyLevel(str(raw).upper())
                    except ValueError:
                        level = None
            if level is not None:
                signals.append(SafetySignal(code=item.name, level=level, source=item.source,
                                            message=str(item.value) if item.value is not None and not isinstance(item.value, dict) else None,
                                            timestamp=item.timestamp, latched=level.rank >= SafetyLevel.P3.rank,
                                            metadata=item.metadata))
        # A feedback frame is a complete snapshot for the *environment*
        # producer, but not for monitors or other producers.  Reconcile only
        # the environment-owned records so a recovered environment condition
        # disappears on the next P0 frame without clearing a monitor's active
        # signal.
        return reconcile_safety_snapshot(
            self,
            "environment",
            feedback.safety_level,
            signals,
            source="environment",
            synthetic_code="environment.level",
        )

    def clear(self, *, code: str | None = None, include_latched: bool = False) -> SafetyStateSnapshot:
        with self._lock:
            changed = False
            if code is not None:
                signal = self._signals.get(code)
                if signal is not None and (include_latched or not signal.latched):
                    self._signals.pop(code, None)
                    changed = True
            elif include_latched:
                changed = bool(self._signals)
                self._signals.clear()
            else:
                retained = {key: signal for key, signal in self._signals.items() if signal.latched}
                changed = len(retained) != len(self._signals)
                self._signals = retained
            if changed:
                self._revision += 1
                self._updated_at = datetime.now(timezone.utc)
            return self._snapshot_unlocked()

    def heartbeat(
        self,
        source: str,
        *,
        ttl_s: float = 3.0,
        failure_level: SafetyLevel | str = SafetyLevel.P3,
    ) -> SafetyStateSnapshot:
        """Register/renew a service heartbeat used for fail-safe detection."""
        if not source or ttl_s <= 0:
            raise ValueError("heartbeat source must be non-empty and ttl_s must be positive")
        level = failure_level if isinstance(failure_level, SafetyLevel) else SafetyLevel(str(failure_level).upper())
        with self._lock:
            was_expired = source in self._expired_heartbeats
            self._heartbeats[source] = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s), level)
            if was_expired:
                self._expired_heartbeats.discard(source)
                self._revision += 1
                self._updated_at = datetime.now(timezone.utc)
            return self._snapshot_unlocked()

    def clear_heartbeat(self, source: str) -> SafetyStateSnapshot:
        with self._lock:
            existed = source in self._heartbeats or source in self._expired_heartbeats
            self._heartbeats.pop(source, None)
            self._expired_heartbeats.discard(source)
            if existed:
                self._revision += 1
                self._updated_at = datetime.now(timezone.utc)
            return self._snapshot_unlocked()

    def recover(
        self,
        level: SafetyLevel | str | None = None,
        *,
        approved: bool = False,
        include_latched: bool | None = None,
    ) -> SafetyStateSnapshot:
        """Clear signals for a recovered level, respecting manual acknowledgement.

        P0-P2 transient conditions may be cleared automatically.  P3/P4 are
        latched by default and require an explicit ``approved=True`` call
        (normally after a human or hardware controller acknowledgement).  A
        lower-level recovery never removes a more severe signal that is still
        active.
        """
        target = self.snapshot().level if level is None else (
            level if isinstance(level, SafetyLevel) else SafetyLevel(str(level).upper())
        )
        if target.rank >= SafetyLevel.P3.rank and not approved:
            raise PermissionError(f"manual acknowledgement required to recover {target.value}")
        clear_latched = target.rank >= SafetyLevel.P3.rank if include_latched is None else include_latched
        with self._lock:
            self._signals = {
                code: signal
                for code, signal in self._signals.items()
                if signal.level.rank > target.rank or (signal.latched and not clear_latched)
            }
            self._revision += 1
            self._updated_at = datetime.now(timezone.utc)
            return self._snapshot_unlocked()


class SafetyGate:
    """Conservative policy checked before every Action."""

    def __init__(self, *, pause_level: SafetyLevel = SafetyLevel.P3,
                 emergency_level: SafetyLevel = SafetyLevel.P4,
                 limited_action_types: set[str] | None = None,
                 require_limited_action_approval: bool = False):
        if emergency_level.rank < pause_level.rank:
            raise ValueError("emergency_level cannot be below pause_level")
        self.pause_level = pause_level
        self.emergency_level = emergency_level
        self.limited_action_types = set(limited_action_types) if limited_action_types is not None else None
        self.require_limited_action_approval = require_limited_action_approval

    def check(self, snapshot: SafetyStateSnapshot, action: Action | None = None) -> dict[str, Any]:
        level = snapshot.level
        if level.rank >= self.emergency_level.rank:
            return {"allowed": False, "mode": "emergency_stop", "reason": "safety_level_p4", "action": action}
        if level.rank >= self.pause_level.rank:
            return {"allowed": False, "mode": "pause", "reason": "safety_level_p3", "action": action}
        if level is SafetyLevel.P2:
            if action is not None and self.limited_action_types is not None:
                action_type = action.action_type.value if isinstance(action.action_type, ActionType) else str(action.action_type)
                if action_type not in self.limited_action_types:
                    return {"allowed": False, "mode": "limited", "reason": "action_type_not_allowed_in_p2", "action": action}
            if action is not None and self.require_limited_action_approval and not bool(action.metadata.get("safety_approved", False)):
                return {"allowed": False, "mode": "limited", "reason": "action_not_approved_in_p2", "action": action}
            return {"allowed": True, "mode": "limited", "reason": "safety_level_p2", "action": action}
        if level is SafetyLevel.P1:
            return {"allowed": True, "mode": "monitor", "reason": "safety_level_p1", "action": action}
        return {"allowed": True, "mode": "normal", "reason": "safety_level_p0", "action": action}


def __getattr__(name):
    if name in {"SQLiteSafetyStateStore", "SharedSafetyStateStore"}:
        from ..storage.safety import SQLiteSafetyStateStore
        return SQLiteSafetyStateStore
    raise AttributeError(name)


class _DefaultSafetyStore:
    """Backward-compatible facade, constructed on first use rather than import."""
    def __init__(self):
        self._instance = None
        self._creation_lock = RLock()

    def __getattr__(self, name):
        with self._creation_lock:
            if self._instance is None:
                path = os.environ.get("ROBOTENON_SAFETY_DB") or os.environ.get("ROBOSYS_SAFETY_DB")
                if path:
                    from ..storage.safety import SQLiteSafetyStateStore
                    self._instance = SQLiteSafetyStateStore(path)
                else:
                    self._instance = SafetyStateStore()
            instance = self._instance
        return getattr(instance, name)


global_safety_state = _DefaultSafetyStore()
GLOBAL_SAFETY_STATE = global_safety_state


__all__ = [
    "SafetyStateStore",
    "SQLiteSafetyStateStore",  # noqa: F822 - lazy compatibility export
    "SharedSafetyStateStore",  # noqa: F822 - lazy compatibility export
    "SafetyGate",
    "SafetyLevel",
    "SafetySignal",
    "SafetyStateSnapshot",
    "reconcile_safety_snapshot",
    "async_safety_call",
    "async_reconcile_safety_snapshot",
    "safety_rank",
    "max_safety_level",
    "safety_mode",
    "global_safety_state",
    "GLOBAL_SAFETY_STATE",
]
