"""Framework shared-model Tool carrying native images/state to HLP or verifier."""
from robotenon.tools.batch_model import SharedBatchModel
from robotenon.tools.base import ToolResult
from robotenon.tools.registry import register_tool
from .codec import unpack_observation


@register_tool('benchmark_model', kind='model')
class BenchmarkModel(SharedBatchModel):
    def __init__(self, *, purpose=None, **kwargs):
        super().__init__(**kwargs)
        self.purpose = purpose

    async def call(self, arguments, context):
        if arguments.get('operation') == 'end_session':
            return await super().call(arguments, context)
        feedback = context.feedback
        if feedback.terminal:
            raise RuntimeError('model requested after benchmark termination')
        view = context.observation
        observation = unpack_observation(feedback.observation, copy=False,
            resolver=(lambda frame: view.get(frame.frame_id)) if view is not None else None)
        payload = {k: v for k, v in arguments.items() if k != 'observation_view'}
        payload['observation'] = observation
        if self.purpose is not None:
            payload['purpose'] = self.purpose
        # Native task identity, not a static task_id from the application config.
        payload['task'] = feedback.metadata['instruction']
        metadata = {'benchmark_id': feedback.metadata['benchmark'],
                    'episode_id': feedback.metadata['native_case'],
                    'task': dict(feedback.metadata['native_task']),
                    'action_spec': feedback.metadata.get('action_spec', {}),
                    'observation_spec': feedback.metadata.get('observation_spec', {})}
        return ToolResult(output=await self.infer(str(context.session_id), payload, metadata))
