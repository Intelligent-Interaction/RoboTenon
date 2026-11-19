"""Cross-process gate for expensive native Worker bootstrap."""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from threading import Lock

_HANDLE = None
_LOCK = Lock()


def acquire_startup_gate(limit: int, root: str | None):
    """Hold one host-wide startup slot until the first environment is ready."""
    global _HANDLE
    if limit < 1 or not root:
        return
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    start = os.getpid() % limit
    while True:
        for offset in range(limit):
            candidate = (start + offset) % limit
            stream = (directory / f"{candidate}.lock").open("a+")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                stream.close()
            else:
                with _LOCK:
                    _HANDLE = stream
                return
        time.sleep(0.02)


def mark_worker_initialized():
    """Release the startup slot after the first native reset and observation."""
    release_startup_gate()


def release_startup_gate():
    """Release a held slot; safe to call repeatedly and on error paths."""
    global _HANDLE
    with _LOCK:
        handle, _HANDLE = _HANDLE, None
    if handle is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
