"""One opt-in retention policy for terminal-run logs, traces and videos.

Never follows symlinks, removes run metadata, or prunes a run with a held lease,
queued resume, or a surviving owned process. Dry-run is the default.
"""

import fcntl
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .processes import owned_processes


@dataclass(frozen=True)
class RetentionPolicy:
    logs_days: int = 30
    traces_days: int = 30
    videos_days: int = 7
    failed_days: int = 90
    min_free_gb: float = 5.0

    def __post_init__(self):
        if any(
            type(getattr(self, key)) is not int or getattr(self, key) < 1
            for key in ("logs_days", "traces_days", "videos_days", "failed_days")
        ):
            raise ValueError("retention days must be positive integers")
        if not isinstance(self.min_free_gb, (int, float)) or self.min_free_gb < 0:
            raise ValueError("min_free_gb must be nonnegative")


@contextmanager
def _terminal_lease(root):
    lock = root / ".control" / "run.lock"
    if not lock.is_file() or lock.is_symlink() or lock.parent.is_symlink():
        yield False
        return
    with lock.open("r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield not (
                root / ".control" / "resume.json"
            ).exists() and not owned_processes(root / ".processes")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _jobs_after(root, after, limit=128):
    database = root / "jobs.sqlite3"
    if database.exists():
        conn = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=1)
        try:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT record_json FROM jobs WHERE job_id>? AND status IN ('completed','failed','cancelled') "
                    "ORDER BY job_id LIMIT ?",
                    (after, limit),
                )
            ]
        finally:
            conn.close()
    legacy = root / "jobs.json"
    values = json.loads(legacy.read_text()) if legacy.exists() else []
    values = values.values() if isinstance(values, dict) else values
    return sorted(
        (
            job
            for job in values
            if job.get("job_id", "") > after
            and job.get("status") in {"completed", "failed", "cancelled"}
        ),
        key=lambda job: job["job_id"],
    )[:limit]


def _days(path, policy, failed=False):
    if path.suffix.lower() in {".mp4", ".webm", ".avi"}:
        days = policy.videos_days
    elif path.suffix == ".log" or path.name in {"stdout.txt", "stderr.txt"}:
        days = policy.logs_days
    elif any(part.endswith(".trace") for part in path.parts) or path.name.startswith(
        ("trace.", "traces.")
    ):
        days = policy.traces_days
    else:
        return None
    return max(days, policy.failed_days) if failed else days


def _safe_child(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        return None
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            return None
    return path


def _walk_slice(conn, root, job_id, policy, report, now, deadline, quota, apply):
    # Persist directory enumeration separately from deletion. Deleting children
    # cannot shift the saved enumeration offset of an unfinished directory.
    if not conn.execute("SELECT 1 FROM scans WHERE job_id=?", (job_id,)).fetchone():
        conn.execute("INSERT INTO scans VALUES (?)", (job_id,))
        conn.execute(
            "INSERT INTO directories(job_id,path,position) VALUES (?,'.',0)", (job_id,)
        )
        conn.execute(
            "INSERT OR IGNORE INTO files VALUES (?,NULL,?)", (job_id, job_id + ".log")
        )
    used = 0
    while used < quota and time.monotonic() < deadline:
        candidates = conn.execute(
            "SELECT directory,path FROM files f WHERE job_id=? AND (directory IS NULL OR "
            "NOT EXISTS (SELECT 1 FROM directories d WHERE d.job_id=f.job_id AND d.path=f.directory)) LIMIT 64",
            (job_id,),
        ).fetchall()
        if candidates:
            for directory, relative in candidates:
                if used >= quota or time.monotonic() >= deadline:
                    break
                used += 1
                path = (
                    root.parent / relative
                    if directory is None
                    else _safe_child(root, relative)
                )
                if path is not None and not path.is_symlink() and path.is_file():
                    days = _days(
                        path.relative_to(root.parent), policy, report["_failed"]
                    )
                    try:
                        stat = path.stat()
                        if days is not None and now - stat.st_mtime >= days * 86400:
                            if apply:
                                path.unlink()
                            report["files"].append(str(path.relative_to(root.parent)))
                            report["bytes"] += stat.st_size
                    except FileNotFoundError:
                        pass
                conn.execute(
                    "DELETE FROM files WHERE job_id=? AND path=?", (job_id, relative)
                )
            continue
        item = conn.execute(
            "SELECT path,position FROM directories WHERE job_id=? ORDER BY seq LIMIT 1",
            (job_id,),
        ).fetchone()
        if item is None:
            conn.execute("DELETE FROM scans WHERE job_id=?", (job_id,))
            return used, False
        relative, position = item
        directory = _safe_child(root, relative)
        if directory is None or not directory.is_dir():
            conn.execute(
                "DELETE FROM directories WHERE job_id=? AND path=?", (job_id, relative)
            )
            continue
        complete = True
        processed_new = False
        try:
            with os.scandir(directory) as entries:
                for number, entry in enumerate(entries):
                    if number < position:
                        continue
                    # Replaying a large directory prefix may itself exceed the
                    # soft deadline. Still advance one entry so a saved cursor
                    # cannot remain stuck forever on slow storage.
                    if used >= quota or (
                        processed_new and time.monotonic() >= deadline
                    ):
                        complete = False
                        break
                    used += 1
                    processed_new = True
                    position = number + 1
                    name = str(Path(relative) / entry.name)
                    if entry.is_symlink() or entry.name in {".control", ".processes"}:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        conn.execute(
                            "INSERT OR IGNORE INTO directories(job_id,path,position) VALUES (?,?,0)",
                            (job_id, name),
                        )
                    elif _days(Path(name), policy) is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO files VALUES (?,?,?)",
                            (job_id, relative, name),
                        )
        except FileNotFoundError:
            pass
        if complete:
            conn.execute(
                "DELETE FROM directories WHERE job_id=? AND path=?", (job_id, relative)
            )
        else:
            conn.execute(
                "UPDATE directories SET position=? WHERE job_id=? AND path=?",
                (position, job_id, relative),
            )
        conn.commit()
    return used, True


def prune_artifacts(
    output_root, policy, *, apply=False, now=None, budget_s=5, max_entries=20000
):
    """Resume bounded, round-robin cleanup; dry runs never advance the saved cursor."""
    if budget_s <= 0 or max_entries < 1:
        raise ValueError("cleanup budgets must be positive")
    output_root = Path(output_root).resolve()
    disk = shutil.disk_usage(output_root)
    report = {
        "applied": apply,
        "files": [],
        "bytes": 0,
        "skipped_runs": [],
        "truncated": False,
        "disk": {
            "free_bytes": disk.free,
            "total_bytes": disk.total,
            "low_space": disk.free < policy.min_free_gb * 1024**3,
        },
    }
    state_path = output_root / ".retention.sqlite3"
    with (output_root / ".retention.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**report, "busy": True}
        conn = sqlite3.connect(state_path if apply else ":memory:", timeout=1)
        try:
            if not apply and state_path.exists():
                saved = sqlite3.connect(f"{state_path.as_uri()}?mode=ro", uri=True)
                try:
                    saved.backup(conn)
                finally:
                    saved.close()
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS scans(job_id TEXT PRIMARY KEY);"
                "CREATE TABLE IF NOT EXISTS directories(seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, path TEXT, position INTEGER, UNIQUE(job_id,path));"
                "CREATE TABLE IF NOT EXISTS files(job_id TEXT, directory TEXT, path TEXT, PRIMARY KEY(job_id,path));"
                "CREATE TABLE IF NOT EXISTS cursor(value TEXT);"
            )
            saved = conn.execute("SELECT value FROM cursor LIMIT 1").fetchone()
            after = saved[0] if saved else ""
            visited, used = set(), 0
            deadline = time.monotonic() + budget_s
            now = time.time() if now is None else now
            while used < max_entries and time.monotonic() < deadline:
                jobs = _jobs_after(output_root, after)
                if not jobs:
                    jobs = _jobs_after(output_root, "")
                if not jobs:
                    break
                for job in jobs:
                    job_id = job["job_id"]
                    if (
                        not isinstance(job_id, str)
                        or Path(job_id).name != job_id
                        or job_id in {".", ".."}
                    ):
                        raise ValueError(
                            "invalid experiment identity in retention registry"
                        )
                    if job_id in visited:
                        return {**report, "inspected": used, "next_job_after": after}
                    if used >= max_entries or time.monotonic() >= deadline:
                        report["truncated"] = True
                        break
                    visited.add(job_id)
                    after = job_id
                    used += 1
                    root = Path(job["output_root"])
                    if (
                        not root.is_symlink()
                        and root.resolve().parent == output_root
                        and root.is_dir()
                    ):
                        root = root.resolve()
                        with _terminal_lease(root) as safe:
                            if safe:
                                report["_failed"] = job.get("status") == "failed"
                                count, pending = _walk_slice(
                                    conn,
                                    root,
                                    job_id,
                                    policy,
                                    report,
                                    now,
                                    deadline,
                                    min(2048, max_entries - used),
                                    apply,
                                )
                                used += count
                                report["truncated"] |= pending
                                report.pop("_failed", None)
                            else:
                                report["skipped_runs"].append(job_id)
                    conn.execute("DELETE FROM cursor")
                    conn.execute("INSERT INTO cursor VALUES (?)", (after,))
                    conn.commit()
            if used >= max_entries or time.monotonic() >= deadline:
                report["truncated"] = True
            return {**report, "inspected": used, "next_job_after": after}
        finally:
            conn.commit()
            conn.close()
