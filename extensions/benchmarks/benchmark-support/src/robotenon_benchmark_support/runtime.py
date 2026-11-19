"""Backend-declared history and action chunks, shared by every benchmark."""

from collections import deque

from robotenon_benchmark_support.codec import unpack_observation

from robotenon.tools.base import ToolResult
from robotenon.tools.batch_model import SharedBatchModel
from robotenon.tools.registry import register_tool


@register_tool("benchmark_backend", kind="model")
class BackendRuntime(SharedBatchModel):
    def __init__(self, *, declaration, **kwargs):
        super().__init__(**kwargs)
        obs_history = declaration["history"]["length"]
        obs_interval = declaration["history"]["interval"]
        self.replan_steps = declaration["replan_steps"]
        if min(obs_history, obs_interval) < 1:
            raise ValueError("history and interval must be positive")
        self.length, self.interval = obs_history, obs_interval
        self.histories, self.actions = {}, {}

    async def call(self, arguments, context):
        session = str(context.session_id)
        if arguments.get("operation") == "end_episode":
            await self.end_session(session, arguments["result"])
            return ToolResult(output={"ended": True})
        view = context.observation
        obs = unpack_observation(
            context.feedback.observation,
            copy=False,
            resolver=(lambda frame: view.get(frame.frame_id))
            if view is not None
            else None,
        )
        history = self.histories.setdefault(
            session, deque(maxlen=(self.length - 1) * self.interval + 1)
        )
        history.append(obs)
        actions = self.actions.setdefault(session, deque())
        if not actions:
            items = list(history)
            selected = [
                items[max(0, len(items) - 1 - (self.length - 1 - i) * self.interval)]
                for i in range(self.length)
            ]
            output = await self.infer(
                session,
                {"history": selected, "task_description": arguments.get("instruction") or obs.get("task_description", "")},
                {
                    "benchmark_id": context.feedback.metadata["benchmark"],
                    "episode_id": context.feedback.metadata["native_case"],
                    "task": dict(context.feedback.metadata["native_task"]),
                    "action_spec": context.feedback.metadata.get("action_spec", {}),
                    "observation_spec": context.feedback.metadata.get(
                        "observation_spec", {}
                    ),
                },
            )
            chunk = output.get("native_actions")
            if not isinstance(chunk, list) or not chunk:
                raise ValueError("backend must return nonempty native_actions")
            actions.extend(chunk[:self.replan_steps])
        action = actions.popleft()
        return ToolResult(output={"native_action": action, "needs_plan": not actions,
                                  "action_queue_remaining": len(actions)})

    async def end_session(self, session_id, result=None):
        self.histories.pop(str(session_id), None)
        self.actions.pop(str(session_id), None)
        await super().end_session(session_id, result)

    async def close(self):
        self.histories.clear()
        self.actions.clear()
        await super().close()
