"""Task provider for official RoboDojo evaluation invocations."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from robotenon.infra.params import Params
from robotenon.task_adapters.base import BaseTaskAdapter, TaskAdapterDescriptor, TaskCase
from robotenon.task_adapters.registry import register_task_adapter

from .runner import OfficialRoboDojoRunner, OfficialRunSpec


@register_task_adapter(
    "robodojo-office",
    version="1.0.0",
    kind="benchmark",
    description="Run RoboDojo through its official XPolicyLab evaluation client.",
    cli_overrides=("task_names", "num_trials", "seed"),
)
class RoboDojoOfficialTaskAdapter(BaseTaskAdapter):
    descriptor = TaskAdapterDescriptor(
        adapter_id="robodojo-office",
        version="1.0.0",
        display_name="RoboDojo Official",
        kind="benchmark",
        capabilities=frozenset({"official_eval", "external_policy", "seed"}),
    )

    def __init__(
        self,
        *,
        task_names: list[str] | tuple[str, ...] | None = None,
        tasks: list[str] | tuple[str, ...] | None = None,
        num_trials: int = 1,
        seed: int = 0,
        robodojo_root: str | None = None,
        policy_name: str = "RoboTenon",
        policy_host: str = "127.0.0.1",
        policy_port: int = 19000,
        checkpoint: str = "external",
        env_config: str = "arx_x5",
        action_type: str = "ee",
        env_gpu: str = "0",
        eval_num: str = "1",
        timeout_s: float = 3600.0,
        result_root: str | None = None,
        **_: Any,
    ) -> None:
        self.task_names = list(task_names or tasks or ["stack_bowls"])
        self.num_trials = int(num_trials)
        self.seed = int(seed)
        self.robodojo_root = robodojo_root or os.environ.get("ROBODOJO_ROOT")
        self.policy_name = policy_name
        self.policy_host = policy_host
        self.policy_port = int(policy_port)
        self.checkpoint = checkpoint
        self.env_config = env_config
        self.action_type = action_type
        self.env_gpu = str(env_gpu)
        self.eval_num = str(eval_num)
        self.timeout_s = float(timeout_s)
        self.result_root = result_root
        self.runner = OfficialRoboDojoRunner()

    def validate(self, params: Params) -> None:
        if not self.task_names or len(self.task_names) != len(set(self.task_names)):
            raise ValueError("RoboDojo task_names must be non-empty and unique")
        if self.num_trials < 1 or self.timeout_s <= 0:
            raise ValueError("num_trials and timeout_s must be positive")
        if not self.robodojo_root:
            raise ValueError("robodojo_root or ROBODOJO_ROOT is required")

    def enumerate_cases(self, *, adapter_id: str, params: Params) -> Iterator[TaskCase]:
        self.validate(params)
        index = 0
        for task_name in self.task_names:
            for episode in range(self.num_trials):
                yield TaskCase(
                    adapter_id=adapter_id,
                    task_id=task_name,
                    environment_id="robodojo-official",
                    index=index,
                    episode=episode,
                    seed=self.seed + index,
                    parameters={"action_type": self.action_type},
                )
                index += 1

    async def run_case(self, case: TaskCase, context) -> dict:
        root = Path(str(self.robodojo_root)).expanduser().resolve()
        return await self.runner.run(OfficialRunSpec(
            root=root,
            task_name=case.task_id,
            policy_name=self.policy_name,
            policy_host=self.policy_host,
            policy_port=self.policy_port,
            checkpoint=self.checkpoint,
            env_config=self.env_config,
            action_type=self.action_type,
            seed=int(case.seed or 0),
            env_gpu=self.env_gpu,
            eval_num=self.eval_num,
            timeout_s=self.timeout_s,
            result_root=Path(self.result_root).expanduser() if self.result_root else None,
        ))


__all__ = ["RoboDojoOfficialTaskAdapter"]
