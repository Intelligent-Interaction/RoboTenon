"""Task-local drafts and recursively immutable, JSON-safe published values."""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping, MutableSequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import RLock

from .serialization import jsonable


class FrozenMap(Mapping):
    def __init__(self, data):
        self._data = {key: freeze(value) for key, value in data.items()}

    @classmethod
    def _from_frozen(cls, data):
        """Build an outer map from values already recursively frozen."""
        instance = cls.__new__(cls)
        instance._data = dict(data)
        return instance

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value)
    if isinstance(value, (tuple, list)):
        return tuple(freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return freeze(jsonable(value))


def thaw(value):
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return deepcopy(value)


_active: ContextVar[Workspace | None] = ContextVar("robotenon_workspace", default=None)
_view: ContextVar[tuple | None] = ContextVar("robotenon_result_view", default=None)
_trigger: ContextVar[tuple | None] = ContextVar("robotenon_trigger", default=None)


def current_trigger(ctx):
    trigger = _trigger.get()
    return trigger[1] if trigger is not None and trigger[0] == id(ctx) else None


@contextmanager
def bind_trigger(ctx, trigger):
    token = _trigger.set((id(ctx), trigger))
    try:
        yield
    finally:
        _trigger.reset(token)


def current_workspace(ctx):
    workspace = _active.get()
    return workspace if workspace is not None and workspace.context_id == id(ctx) else None


def current_view(ctx):
    view = _view.get()
    return view if view is not None and view[0] == id(ctx) else None


@contextmanager
def result_view(ctx, scope, records, fields):
    # Do not inherit a parent's private workspace into a nested graph scheduler.
    ws_token = _active.set(None)
    token = _view.set((id(ctx), scope, records, fields))
    try:
        yield
    finally:
        _view.reset(token)
        _active.reset(ws_token)


@contextmanager
def bind_workspace(workspace):
    token = _active.set(workspace)
    try:
        yield workspace
    finally:
        workspace.close()
        _active.reset(token)


class DraftMap(MutableMapping):
    def __init__(self, workspace, data):
        self._workspace, self._data = workspace, data

    def __getitem__(self, key):
        with self._workspace.lock:
            self._workspace.check()
            return self._workspace.wrap(self._data[key])

    def __setitem__(self, key, value):
        with self._workspace.lock:
            self._workspace.check()
            self._data[key] = thaw(value)

    def __delitem__(self, key):
        with self._workspace.lock:
            self._workspace.check()
            del self._data[key]

    def __iter__(self):
        with self._workspace.lock:
            self._workspace.check()
            return iter(tuple(self._data))

    def __len__(self):
        self._workspace.check()
        return len(self._data)

    def __deepcopy__(self, memo):
        with self._workspace.lock:
            self._workspace.check()
            return deepcopy(self._data, memo)


class DraftList(MutableSequence):
    def __init__(self, workspace, data):
        self._workspace, self._data = workspace, data

    def __getitem__(self, key):
        with self._workspace.lock:
            self._workspace.check()
            return self._workspace.wrap(self._data[key])

    def __setitem__(self, key, value):
        with self._workspace.lock:
            self._workspace.check()
            self._data[key] = thaw(value)

    def __delitem__(self, key):
        with self._workspace.lock:
            self._workspace.check()
            del self._data[key]

    def __len__(self):
        self._workspace.check()
        return len(self._data)

    def insert(self, index, value):
        with self._workspace.lock:
            self._workspace.check()
            self._data.insert(index, thaw(value))

    def __deepcopy__(self, memo):
        with self._workspace.lock:
            self._workspace.check()
            return deepcopy(self._data, memo)


# These application fields retain their typed API, but writes are staged and
# conflicting sibling assignments are rejected by the commit coordinator.
BUSINESS_FIELDS = frozenset({
    "session_id", "task_id", "task_item", "feedback", "action", "action_chunk",
    "body_result", "monitor_report", "decision", "safety",
})


class Workspace:
    def __init__(self, ctx, frame, records, fields):
        self.context_id = id(ctx)
        self.path = frame.path
        self.node = frame.node
        self.version = frame.activation
        self.step = frame.step
        self.records = records
        self.previous = records.get(frame.node, {}).get("values", FrozenMap({}))
        self.fields = fields
        self.outputs = fields.get("_outputs", FrozenMap({}))
        self.changes = {}
        self.output = None
        self.inputs = freeze(object.__getattribute__(ctx, "values"))
        self._data = {}
        self.values = DraftMap(self, self._data)
        self.lock = RLock()
        self.open = True

    def check(self):
        if not self.open:
            raise RuntimeError("node workspace is closed")
        if _active.get() is not self:
            raise RuntimeError("node workspace accessed without its execution context")

    def wrap(self, value):
        if isinstance(value, dict):
            return DraftMap(self, value)
        if isinstance(value, list):
            return DraftList(self, value)
        # Mutable models/opaque objects are never handed out by reference.
        return deepcopy(value)

    def get_field(self, name):
        self.check()
        return deepcopy(self.changes.get(name, self.fields.get(name)))

    def set_field(self, name, value):
        self.check()
        self.changes[name] = deepcopy(value)

    def prepare(self):
        with self.lock:
            self.check()
            # JSON validation happens before any shared state is changed.
            values = freeze(jsonable(self._data))
            output = deepcopy(self.output)
            jsonable(output)
            return {"version": self.version, "path": list(self.path), "step": self.step, "values": values, "output": jsonable(output)}, output, deepcopy(self.changes)

    def close(self):
        with self.lock:
            self.open = False
