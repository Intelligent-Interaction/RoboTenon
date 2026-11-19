"""Low-overhead file-descriptor snapshots for long-running experiments."""

from __future__ import annotations

import json
import os
import resource
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .processes import process_table


def _fd_kind(target: str) -> str:
    if target.startswith("socket:"):
        return "socket"
    if target.startswith("pipe:"):
        return "pipe"
    if target.startswith("anon_inode:"):
        return target.split("[", 1)[0]
    if target.endswith(" (deleted)"):
        return "deleted"
    if target.startswith("/dev/"):
        return "device"
    if target.startswith("/"):
        return "file"
    return "other"


def _registered_roles(directory: Path) -> dict[int, str]:
    roles: dict[int, str] = {}
    for path in directory.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            roles[int(value["pid"])] = str(value.get("role") or "descendant")
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return roles


class FileDescriptorMonitor:
    """Append process-tree FD counts and per-snapshot deltas as JSONL."""

    def __init__(self, output_root: str | Path):
        self.output_root = Path(output_root)
        self.path = self.output_root / "fd-usage.jsonl"
        self.registry = self.output_root / ".processes"
        self._previous: dict[int, int] = {}

    def _descendants(self) -> dict[int, dict[str, Any]]:
        table = process_table()
        selected = {os.getpid()}
        while True:
            added = {
                pid
                for pid, row in table.items()
                if row["ppid"] in selected and pid not in selected
            }
            if not added:
                break
            selected.update(added)
        return {pid: table[pid] for pid in selected if pid in table}

    def snapshot(
        self,
        event: str,
        *,
        episode: Mapping[str, Any] | None = None,
        timestamp: str,
    ) -> dict[str, Any]:
        roles = _registered_roles(self.registry)
        rows = []
        current: dict[int, int] = {}
        processes = self._descendants()
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        for pid in sorted(processes):
            fd_root = Path("/proc") / str(pid) / "fd"
            if pid == os.getpid() and not fd_root.is_dir():
                fd_root = Path("/dev/fd")
            try:
                descriptors = list(fd_root.iterdir())
            except (FileNotFoundError, PermissionError, OSError):
                continue
            kinds: Counter[str] = Counter()
            for descriptor in descriptors:
                try:
                    kinds[_fd_kind(os.readlink(descriptor))] += 1
                except OSError:
                    if pid != os.getpid():
                        continue
                    try:
                        mode = os.fstat(int(descriptor.name)).st_mode
                    except (OSError, ValueError):
                        continue
                    if stat.S_ISSOCK(mode):
                        kinds["socket"] += 1
                    elif stat.S_ISFIFO(mode):
                        kinds["pipe"] += 1
                    elif stat.S_ISCHR(mode):
                        kinds["device"] += 1
                    elif stat.S_ISREG(mode):
                        kinds["file"] += 1
                    else:
                        kinds["other"] += 1
            count = len(descriptors)
            current[pid] = count
            try:
                command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
                command_text = command.replace(b"\0", b" ").decode(errors="replace")[
                    :500
                ]
            except OSError:
                command_text = " ".join(sys.argv)[:500] if pid == os.getpid() else ""
            role = roles.get(pid)
            if pid == os.getpid():
                role = "coordinator+trace+metrics"
            elif role is None and "spawn_main" in command_text:
                role = "worker"
            elif role is None:
                role = "descendant"
            rows.append(
                {
                    "pid": pid,
                    "ppid": processes[pid]["ppid"],
                    "role": role,
                    "count": count,
                    "delta": count - self._previous.get(pid, count),
                    "types": dict(sorted(kinds.items())),
                    "command": command_text,
                }
            )
        self._previous = current
        coordinator_count = current.get(os.getpid(), 0)
        payload = {
            "timestamp": timestamp,
            "event": event,
            "episode": dict(episode or {}),
            "soft_limit": soft_limit,
            "hard_limit": hard_limit,
            "coordinator_count": coordinator_count,
            "coordinator_ratio": coordinator_count / soft_limit
            if soft_limit not in {-1, resource.RLIM_INFINITY} and soft_limit > 0
            else None,
            "warning": bool(
                soft_limit not in {-1, resource.RLIM_INFINITY}
                and soft_limit > 0
                and coordinator_count / soft_limit >= 0.8
            ),
            "processes": rows,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        return payload
