"""Process runner for RoboDojo's supported ``robodojo.sh client`` entry."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .results import find_result_file, project_result


@dataclass(frozen=True, slots=True)
class OfficialRunSpec:
    root: Path
    task_name: str
    policy_name: str
    policy_host: str
    policy_port: int
    checkpoint: str = "external"
    env_config: str = "arx_x5"
    action_type: str = "ee"
    seed: int = 0
    env_gpu: str = "0"
    eval_num: str = "1"
    timeout_s: float = 3600.0
    result_root: Path | None = None
    extra_env: Mapping[str, str] | None = None


class OfficialRoboDojoRunner:
    """Run the unmodified official evaluation client and collect its result."""

    def command(self, spec: OfficialRunSpec) -> list[str]:
        entry = spec.root / "scripts" / "robodojo.sh"
        if not entry.is_file():
            raise FileNotFoundError(f"RoboDojo entry point not found: {entry}")
        return [
            "bash", str(entry), "client",
            "--task", spec.task_name,
            "--env-cfg", spec.env_config,
            "--policy-name", spec.policy_name,
            "--policy-host", spec.policy_host,
            "--policy-port", str(spec.policy_port),
            "--ckpt", spec.checkpoint,
            "--action-type", spec.action_type,
            "--seed", str(spec.seed),
            "--env-gpu", str(spec.env_gpu),
            "--eval-num", str(spec.eval_num),
        ]

    async def run(self, spec: OfficialRunSpec) -> dict:
        command = self.command(spec)
        env = dict(os.environ)
        env.update(dict(spec.extra_env or {}))
        run_id = f"robotenon-{uuid4().hex}"
        env["ROBODOJO_RUN_ID"] = run_id
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=spec.root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), spec.timeout_s)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 10.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        text = output.decode("utf-8", errors="replace")
        if process.returncode != 0:
            tail = "\n".join(text.splitlines()[-80:])
            raise RuntimeError(f"RoboDojo client exited {process.returncode}:\n{tail}")
        result_root = spec.result_root or spec.root / "eval_result"
        result_path = find_result_file(
            result_root,
            task_name=spec.task_name,
            policy_name=spec.policy_name,
            run_id=run_id,
        )
        result = project_result(result_path)
        result["process"] = {"returncode": process.returncode, "log_tail": text[-16000:]}
        return result


__all__ = ["OfficialRoboDojoRunner", "OfficialRunSpec"]
