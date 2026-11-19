"""Persist process ownership so cleanup survives coordinator failure."""
from contextvars import ContextVar
import json
import os
from pathlib import Path
import signal
import subprocess
import time


LEASE_FDS = ContextVar("robotenon_resource_fds", default=None)


def resource_fds():
    current = LEASE_FDS.get()
    return tuple(current if current is not None else json.loads(os.environ.get("ROBOTENON_RESOURCE_FDS", "[]")))


def _detach_fd(wrapper):
    return wrapper.detach()


class SharedDescriptor:
    def __init__(self, fd):
        self.fd = fd

    def __reduce__(self):
        # DupFd must run inside spawn's serializer, where pass_fds is recorded.
        from multiprocessing.reduction import DupFd
        return _detach_fd, (DupFd(self.fd),)


def process_table():
    if Path("/proc/self/stat").exists():
        # /proc works in minimal Linux containers without a procps package.
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        rows = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
                if stat[0] in {"Z", "X"}:
                    continue
                pid = int(path.name)
                rows[pid] = {"pid": pid, "ppid": int(stat[1]), "pgid": int(stat[2]),
                             "start": f"{boot}:{stat[19]}"}
            except (OSError, ValueError, IndexError):
                continue
        return rows
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,lstart=,stat="],
                            capture_output=True, text=True, check=True, timeout=2)
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        pid, ppid, pgid = map(int, parts[:3])
        if parts[8].startswith("Z"):
            continue
        rows[pid] = {"pid": pid, "ppid": ppid, "pgid": pgid, "start": " ".join(parts[3:8])}
    return rows


def register_process(directory, pid, *, role):
    row = process_table().get(pid)
    if row is None:
        return None
    row["role"] = role
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(row))
    temporary.replace(path)
    return row


def owned_processes(directory, *, table=None):
    paths = list(Path(directory).glob("*.json"))
    if not paths:
        return {}
    table = process_table() if table is None else table
    owned = {}
    for path in paths:
        record = json.loads(path.read_text())
        row = table.get(record["pid"])
        if row is not None and row["start"] == record["start"] and row["pgid"] == record["pgid"]:
            owned[row["pid"]] = row
    # Include descendants and members of groups positively identified above.
    # Never signal a stale PGID merely because it appears in an old file.
    while True:
        groups = {row["pgid"] for row in owned.values() if row["pgid"] != os.getpgrp()}
        extra = {pid: row for pid, row in table.items()
                 if row["ppid"] in owned or row["pgid"] in groups}
        if extra.keys() <= owned.keys():
            return owned
        owned.update(extra)


def same_process(left, right):
    return (left is not None and right is not None and
            all(left[key] == right[key] for key in ("pid", "pgid", "start")))


def remember_descendants(directory):
    for row in owned_processes(directory).values():
        register_process(directory, row["pid"], role="descendant")


def terminate_owned(directory, *, timeout_s=1.0):
    known = owned_processes(directory)
    forced = False
    def signal_known(sig):
        current = process_table()
        groups = set()
        for pid, row in list(known.items()):
            if pid == os.getpid() or not same_process(current.get(pid), row):
                continue
            try:
                if row["pgid"] != os.getpgrp() and row["pgid"] not in groups:
                    os.killpg(row["pgid"], sig)
                    groups.add(row["pgid"])
                elif row["pgid"] == os.getpgrp():
                    os.kill(pid, sig)
            except ProcessLookupError:
                pass
    signal_known(signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        table = process_table()
        known.update(owned_processes(directory, table=table))
        if not any(same_process(table.get(pid), row) for pid, row in known.items()):
            return {"terminated": True, "forced": forced, "remaining_pids": []}
        time.sleep(.03)
    forced = True
    # Refresh before each signal: child creation during graceful shutdown is
    # still part of the same execution domain.
    known.update(owned_processes(directory))
    signal_known(signal.SIGKILL)
    deadline = time.monotonic() + 1.0
    while True:
        table = process_table()
        remaining = [pid for pid, row in known.items() if same_process(table.get(pid), row)]
        if not remaining or time.monotonic() >= deadline:
            return {"terminated": not remaining, "forced": forced, "remaining_pids": remaining}
        time.sleep(.03)
