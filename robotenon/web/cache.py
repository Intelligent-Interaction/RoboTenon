"""Thread-safe LRU caches with explicit entry and estimated-byte budgets."""

import threading
from collections import OrderedDict


class BoundedCache:
    def __init__(self, max_entries=256, max_bytes=32 * 1024 * 1024):
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("cache budgets must be positive")
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._items = OrderedDict()
        self._lock = threading.RLock()
        self.bytes = 0

    def get(self, key, default=None):
        with self._lock:
            if key not in self._items:
                return default
            self._items.move_to_end(key)
            return self._items[key][0]

    def put(self, key, value, *, size=1):
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self.bytes -= old[1]
            if size > self.max_bytes:
                return
            self._items[key] = value, size
            self.bytes += size
            while len(self._items) > self.max_entries or self.bytes > self.max_bytes:
                _, (_, removed) = self._items.popitem(last=False)
                self.bytes -= removed

    def __setitem__(self, key, value):
        self.put(key, value)

    def __len__(self):
        with self._lock:
            return len(self._items)

    def pop(self, key, default=None):
        with self._lock:
            item = self._items.pop(key, None)
            if item is None:
                return default
            self.bytes -= item[1]
            return item[0]

    def clear(self):
        with self._lock:
            self._items.clear()
            self.bytes = 0
