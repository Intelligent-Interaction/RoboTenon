"""Exclusive run ownership and durable protection against accidental overwrite."""
from functools import wraps
import fcntl
import json
from pathlib import Path


class RunReservation:
    def __init__(self, output_dir, run_id, config_hash):
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("run_id must be a single nonempty directory name")
        self.root = Path(output_dir).resolve() / run_id
        self.config_hash = config_hash
        self.handle = None
        self._context_token = None

    def acquire(self):
        control = self.root / ".control"
        control.mkdir(parents=True, exist_ok=True)
        self.handle = (control / "run.lock").open("a+")
        try:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("run_id is already owned by an active experiment") from None
            claim = control / "run.json"
            status = self.root / "status.json"
            previous = json.loads(claim.read_text()) if claim.exists() else (
                json.loads(status.read_text()) if status.exists() else None)
            if previous is not None:
                if not (control / "resume.json").is_file():
                    raise ValueError("run_id already exists; use explicit rerun/resume or a new run_id")
                if previous.get("config_hash") != self.config_hash:
                    raise ValueError("resume configuration does not match the original run")
            temporary = claim.with_suffix(".tmp")
            temporary.write_text(json.dumps({"config_hash": self.config_hash}) + "\n")
            temporary.replace(claim)
            from .processes import LEASE_FDS, resource_fds
            self._context_token = LEASE_FDS.set((*resource_fds(), self.handle.fileno()))
        except BaseException:
            self.close()
            raise
        return self

    def close(self):
        if self._context_token is not None:
            from .processes import LEASE_FDS
            LEASE_FDS.reset(self._context_token)
            self._context_token = None
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def reserve_evaluation(function):
    @wraps(function)
    async def wrapped(*args, _reservation=None, **kwargs):
        reservation = _reservation or RunReservation(kwargs["output_dir"], kwargs["run_id"],
                                                     kwargs["params"].content_hash).acquire()
        try:
            if reservation.handle is None:
                raise RuntimeError("run reservation is not active")
            return await function(*args, **kwargs)
        finally:
            if _reservation is None:
                reservation.close()
    return wrapped
