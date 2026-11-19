"""Local coordinator control mailbox, serialized against coordinator shutdown."""
from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def control_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
