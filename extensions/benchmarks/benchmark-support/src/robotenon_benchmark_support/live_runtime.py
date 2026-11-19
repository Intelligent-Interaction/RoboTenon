"""Clocked Graph ticks with asynchronous model messages and embodiment-owned hold.

The simulator remains owned by Graph/Body. Background work only invokes the model.
Observation messages are FIFO, as in the upstream non-CI PredictModelServer;
multiple completed action messages before a tick collapse to the newest action.
"""
import asyncio
from collections import deque
import time

from robotenon.runtime.batch_transport import BatchClient
from robotenon.tools.base import ToolResult
from robotenon.tools.registry import register_tool
from .codec import unpack_observation
from .runtime import BackendRuntime
from .modules import validate_native_action


@register_tool('benchmark_live_backend', kind='model')
class LiveBackendRuntime(BackendRuntime):
    def __init__(self, *, live_config, **kwargs):
        super().__init__(**kwargs)
        self.hz = live_config.get('hz', 10.0)
        self.pace = live_config.get('pace', 1.0)
        self.wait_first = live_config.get('wait_first_action', False)
        self.live = {}

    def _submit(self, session, context):
        state = self.live[session]
        feedback = context.feedback
        if feedback.sequence_no == state.get('sequence'):
            return
        state['sequence'] = feedback.sequence_no
        view = context.observation
        obs = unpack_observation(feedback.observation, copy=False,
            resolver=(lambda frame: view.get(frame.frame_id)) if view is not None else None)
        history = self.histories.setdefault(session, deque(maxlen=(self.length-1)*self.interval+1))
        history.append(obs)
        items = list(history)
        selected = [items[max(0, len(items)-1-(self.length-1-i)*self.interval)] for i in range(self.length)]
        state['observations'].append({'history': selected, 'task_description': obs.get('task_description', '')})
        state['wake'].set()

    async def _pump(self, session, metadata):
        state = self.live[session]
        chunk = deque()
        try:
            while True:
                await state['wake'].wait()
                if state.get('closing'):
                    return
                while state['observations']:
                    observation = state['observations'].popleft()
                    if not chunk:
                        output = await self.infer(session, observation, metadata)
                        if state.get('closing'):
                            return
                        actions = output.get('native_actions')
                        if not isinstance(actions, list) or not actions:
                            raise ValueError('live backend must return nonempty native_actions')
                        for action in actions[:self.replan_steps]:
                            validate_native_action(action)
                            chunk.append(action)
                    state['latest'] = chunk.popleft()
                    state['fresh'] = True
                    state['updates'] += 1
                    state['first'].set()
                    await asyncio.sleep(0)
                state['wake'].clear()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state['error'] = error
            state['first'].set()

    async def _begin(self, session, context):
        await self.start()
        metadata = context.feedback.metadata
        task_info = {'benchmark_id': metadata['benchmark'], 'episode_id': metadata['native_case'],
            'task': dict(metadata['native_task']), 'action_spec': metadata.get('action_spec', {}),
            'observation_spec': metadata.get('observation_spec', {}), 'mode': 'live'}
        client = BatchClient(self.endpoint, timeout_s=self.timeout_s, history_cache=self.history_cache)
        self.clients[session] = client
        try:
            await client.open(task_info)
        except BaseException:
            self.clients.pop(session, None)
            raise
        state = self.live[session] = {'observations': deque(), 'wake': asyncio.Event(),
            'first': asyncio.Event(), 'latest': None, 'fresh': False, 'updates': 0,
            'stale': 0, 'step_times': [], 'error': None, 'start': time.monotonic()}
        self._submit(session, context)
        state['pump'] = asyncio.create_task(self._pump(session, task_info))
        if self.wait_first:
            await asyncio.wait_for(state['first'].wait(), timeout=30)
        return state

    async def call(self, arguments, context):
        session = str(context.session_id)
        operation = arguments.get('operation')
        if operation == 'end_episode':
            state = self.live.get(session)
            metrics = self._metrics(state) if state is not None else {}
            result = dict(arguments['result'], rt_metrics=metrics, elapsed_sec=metrics.get('elapsed_sec'))
            await self.end_session(session, result)
            return ToolResult(output={'ended': True, 'rt_metrics': metrics})
        state = self.live.get(session)
        if state is None:
            state = await self._begin(session, context)
        if state['error'] is not None:
            raise state['error']
        if operation == 'after_step':
            feedback = context.feedback
            state['step_times'].append(float(feedback.info.get('step_wall_sec', 0)))
            # Upstream skips observation/pacing on native terminal; a runner
            # horizon still sends its final observation and performs its wait.
            if feedback.info.get('reason') != 'environment_terminal':
                self._submit(session, context)
                remaining = state['tick_start'] + 1/self.hz - time.monotonic()
                await asyncio.sleep(max(0, remaining)/self.pace)
            return ToolResult(output={'rt_metrics': self._metrics(state)})
        if context.feedback.terminal:
            raise RuntimeError('live action requested after terminal')
        state['tick_start'] = time.monotonic()
        stale = not state['fresh']
        if stale:
            state['stale'] += 1
            action = context.feedback.metadata['hold_action']
        else:
            action = state['latest']
            state['fresh'] = False
        return ToolResult(output={'native_action': action, 'live_stale': stale})

    @staticmethod
    def _metrics(state):
        elapsed = time.monotonic() - state['start']
        updates, stale = state['updates'], state['stale']
        times = state['step_times']
        return {'update_count': updates, 'stale_count': stale,
            'stale_action_ratio': stale/(updates+stale) if updates+stale else 0.0,
            'effective_control_hz': updates/elapsed if elapsed else 0.0,
            'step_time_mean': sum(times)/len(times) if times else 0.0,
            'step_time_max': max(times, default=0.0), 'elapsed_sec': round(elapsed, 3)}

    async def end_session(self, session_id, result=None):
        state = self.live.pop(str(session_id), None)
        if state is not None:
            state['closing'] = True
            state['observations'].clear()
            state['wake'].set()
            try:
                await asyncio.wait_for(state['pump'], timeout=self.timeout_s)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                state['pump'].cancel()
                await asyncio.gather(state['pump'], return_exceptions=True)
        await super().end_session(session_id, result)
        if state is not None and state['error'] is not None:
            raise state['error']

    async def close(self):
        results = await asyncio.gather(*(self.end_session(session, {'status': 'aborted'})
            for session in list(self.live)), return_exceptions=True)
        await super().close()
        for result in results:
            if isinstance(result, BaseException):
                raise result
