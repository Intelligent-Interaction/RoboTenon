"""Optional factory-based policy Tool, independent of any model library.

The factory returns an object with reset(task), predict(observation), and
optional close(). Each Worker owns its policy; interleaved sessions are rejected.
"""
from __future__ import annotations

import inspect

from robotenon.tools.base import BaseTool, ToolResult
from robotenon.tools.registry import register_tool
from .codec import unpack_observation
from .integration import load_class


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


@register_tool("native_benchmark_callable", kind="model")
class BenchmarkCallable(BaseTool):
    def __init__(self, *, factory, params=None):
        self.factory, self.params = factory, dict(params or {})
        self.policy = None
        self.session_id = None

    async def start(self):
        if self.policy is None:
            self.policy = await maybe_await(load_class(self.factory)(**self.params))

    async def call(self, arguments, context):
        await self.start()
        session = str(context.session_id)
        if self.session_id is not None and self.session_id != session:
            raise RuntimeError("stateful benchmark policy cannot interleave sessions")
        if self.session_id is None:
            await maybe_await(self.policy.reset(dict(context.feedback.metadata)))
            self.session_id = session
        view = context.observation
        resolver = (lambda frame: view.get(frame.frame_id)) if view is not None else None
        obs = unpack_observation(context.feedback.observation, resolver=resolver)
        action = await maybe_await(self.policy.predict(obs))
        return ToolResult(output={"native_action": action})

    async def end_session(self, session_id):
        if self.session_id == str(session_id):
            hook = getattr(self.policy, "end_episode", None)
            try:
                if hook:
                    await maybe_await(hook())
            finally:
                self.session_id = None

    async def close(self):
        try:
            hook = getattr(self.policy, "close", None)
            if hook:
                await maybe_await(hook())
        finally:
            self.policy = None
            self.session_id = None



@register_tool("native_benchmark_vla", kind="model")
class BenchmarkVLA(BenchmarkCallable):
    """Upstream-style start_episode → act → end_episode lifecycle.

Both fake and real factories use exactly this Tool and native message schema.
No network or model library is required by the adapter.
"""
    async def call(self, arguments, context):
        await self.start()
        session = str(context.session_id)
        if self.session_id is not None and self.session_id != session:
            raise RuntimeError('stateful VLA cannot interleave sessions')
        if arguments.get('operation') == 'end_episode':
            if self.session_id != session:
                raise RuntimeError('end_episode without an active VLA session')
            result = arguments['result']
            await maybe_await(self.policy.end_episode(result))
            self.session_id = None
            return ToolResult(output={'ended': True})
        if self.session_id is None:
            self.session_id = session
            await maybe_await(self.policy.start_episode({
                'task': dict(context.feedback.metadata.get('native_task', {})),
                'action_spec': context.feedback.metadata.get('action_spec', {}),
                'observation_spec': context.feedback.metadata.get('observation_spec', {}),
            }))
        view = context.observation
        resolver = (lambda frame: view.get(frame.frame_id)) if view is not None else None
        obs = unpack_observation(context.feedback.observation, resolver=resolver)
        action = await maybe_await(self.policy.act(obs))
        return ToolResult(output={'native_action': action})

    async def end_session(self, session_id):
        if self.session_id == str(session_id):
            try:
                await maybe_await(self.policy.end_episode({
                    'status': 'aborted', 'metrics': {}, 'steps': None}))
            finally:
                self.session_id = None


from robotenon.tools.batch_model import SharedBatchModel


@register_tool("native_benchmark_batch_vla", kind="model")
class BenchmarkBatchVLA(SharedBatchModel):
    """Native messages over the framework's shared model service."""
    async def call(self, arguments, context):
        if arguments.get('operation') == 'end_episode':
            await self.end_session(context.session_id, arguments['result'])
            return ToolResult(output={'ended': True})
        feedback = context.feedback
        view = context.observation
        resolver = (lambda frame: view.get(frame.frame_id)) if view is not None else None
        observation = unpack_observation(feedback.observation, resolver=resolver)
        action = await self.infer(context.session_id, observation, {
            'task': dict(feedback.metadata.get('native_task', {})),
            'action_spec': feedback.metadata.get('action_spec', {}),
            'observation_spec': feedback.metadata.get('observation_spec', {}),
        })
        return ToolResult(output={'native_action': action})
