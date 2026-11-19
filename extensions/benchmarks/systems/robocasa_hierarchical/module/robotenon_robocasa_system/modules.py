"""RoboCasa hierarchical modules built around the reference XR365 Graph."""
import json
from collections.abc import Mapping
from types import SimpleNamespace

from robotenon.modules.base import BaseModule
from robotenon.modules.verify import VerifyModule
from robotenon.runtime.registry import register_module
from robotenon_benchmark_support.modules import BenchmarkPolicy, BenchmarkTerminal


def _task_text(ctx):
    feedback = getattr(ctx, 'feedback', None)
    state = dict(feedback.state or {}) if feedback is not None else {}
    metadata = dict(feedback.metadata or {}) if feedback is not None else {}
    return str(
        state.get('annotation.human.task_description')
        or metadata.get('instruction')
        or ctx.params.get('task.task_id', [str, None])
        or ''
    )


def _short_subgoal(value, fallback, max_words=20, max_chars=160):
    """Keep the VLA instruction atomic without inventing replacement wording."""
    text = ' '.join(str(value or '').split()).strip()
    if not text:
        return fallback
    if len(text) > max_chars or len(text.split()) > max_words or '\n' in str(value):
        return fallback
    return text


@register_module('robocasa_system_plan', kind='planner')
class Plan(BaseModule):
    """Generic keep/advance/recover planner with persistent subgoal state.

    The policy is task agnostic.  It reduces reasoner traffic by reusing an
    exact subgoal between visual progress checks and only permits the reasoner
    to replace it after an explicit advance or recovery decision.
    """

    async def run(self, ctx):
        mode = ctx.params.get('policy.planner_mode', [str, None])
        if mode in {'contextual', 'advisory'}:
            from .contextual import plan
            return await plan(ctx, advisory=mode == 'advisory')
        from robotenon.contracts.workspace import thaw

        values = thaw(ctx.inputs)
        task = _task_text(ctx)
        if not task:
            raise ValueError('robocasa_system_plan requires a task instruction')
        tool_name = ctx.params.get('model.hlp_tool', [str, None])
        if not tool_name:
            raise ValueError('missing required parameter: model.hlp_tool')

        source = values.get('stm')
        stm = dict(source) if isinstance(source, Mapping) else {}
        current = str(stm.get('current_subgoal') or '').strip()
        age = max(0, int(stm.get('subgoal_age_chunks', 0) or 0))
        since_check = max(0, int(stm.get('chunks_since_progress_check', 0) or 0))
        checks = max(0, int(stm.get('progress_checks', 0) or 0))
        interval = max(1, int(ctx.params.get(
            'policy.progress_check_interval_chunks', [int, 3]
        ) or 3))

        # The first plan always uses vision.  Afterwards, execute the exact
        # same subgoal for `interval` chunks before asking for another visual
        # progress decision.
        should_check = not current or since_check >= interval
        planner_metadata = {}
        if should_check:
            schema = {
                'status': 'continue|advance|recover',
                'subgoal': 'one observable object-level action, at most 20 words',
            }
            prompt = (
                'Control a robot through one atomic object-level subgoal. '
                'This policy is identical for every task. '
                'The images are explicitly labelled as current and, when available, previous. '
                'Return status=continue when the current subgoal is still appropriate and '
                'making progress; its subgoal field must repeat Current subgoal verbatim. '
                'Return status=advance only when visual evidence shows the current subgoal '
                'is complete, then provide the next atomic subgoal. '
                'Return status=recover only when the views show no useful progress or a bad '
                'state, then provide one corrective atomic subgoal. '
                'Do not claim that the whole task is finished: the environment owns the final '
                'success signal. Do not emit robot functions, action vectors, explanations, '
                'multiple steps, markdown, completed lists, or reasoning. Preserve object and '
                'action wording from the original task whenever applicable. Return exactly '
                'one JSON object with exactly the schema keys.\n'
                f'Original task: {task}\n'
                f'Current subgoal: {current or "none; choose the first subgoal"}\n'
                f'Current subgoal age in action chunks: {age}\n'
                f'Schema: {json.dumps(schema)}'
            )
            result = await ctx.call_tool(tool_name, {
                'task': task,
                'prompt': prompt,
                'system_prompt': (
                    'You are a visual high-level robot controller. Return one small JSON '
                    'object only; do not include analysis or markdown.'
                ),
                'image_roles': ['external', 'wrist'],
            })
            if not result.ok:
                raise RuntimeError(f'{tool_name} Tool failed: {result.error or "unknown error"}')
            output = result.output if isinstance(result.output, Mapping) else {}
            status = str(output.get('status') or '').lower()
            if status not in {'continue', 'advance', 'recover'}:
                status = 'continue' if current else 'advance'
            proposed = _short_subgoal(output.get('subgoal'), current or task)
            if status == 'continue' and current:
                subgoal = current
                next_age = age
            else:
                subgoal = proposed
                next_age = 0
            checks += 1
            next_since_check = 0
            planner_metadata = result.metadata
        else:
            status = 'continue'
            subgoal = current
            next_age = age
            next_since_check = since_check

        next_age += 1
        next_since_check += 1
        stm.update({
            'goal': task,
            'current_subgoal': subgoal,
            'subgoal_age_chunks': next_age,
            'chunks_since_progress_check': next_since_check,
            'progress_checks': checks,
            'last_planner_status': status,
        })
        normalized = {'status': status, 'subgoal': subgoal}
        result_values = {
            'stm': stm,
            'instruction': subgoal,
            # Final success belongs to the benchmark environment, so this
            # planner never terminates the Graph itself.
            'planner_decision': 'act',
            'planner_result': normalized,
            'hlp_success': False,
        }
        ctx.values.update(result_values)
        return {**result_values, 'plan': normalized, 'planner': planner_metadata}


@register_module('robocasa_system_action', kind='agent')
class Action(BenchmarkPolicy):
    """One native action per turn keeps every intermediate observation in history."""


class _Context:
    def __init__(self, context, params):
        self.context, self.params = context, params

    def __getattr__(self, name):
        return getattr(self.context, name)


@register_module('robocasa_system_verify', kind='evaluator')
class Verify(VerifyModule):
    async def run(self, ctx):
        if ctx.params.get('policy.verify_enabled', [bool, None]) is False:
            needs_plan = bool(ctx.inputs.get('needs_plan', True))
            result = {
                'stm': dict(ctx.inputs.get('stm') or {}),
                # Preserve the VLA runtime's action-queue signal. While a
                # cached chunk still has actions, routing returns directly to
                # the action node. The HLP is called only after exhaustion.
                'needs_plan': needs_plan,
                'verification': None,
                'verifier_result': None,
                'subtask_success': None,
                'skipped': True,
                'reason': 'verifier_disabled',
            }
            ctx.values.update(result)
            return result
        # Keep the framework verifier's STM and chunk gating; allow a separate
        # model Tool without changing global Params or other concurrent episodes.
        tool = ctx.params.get('model.verify_tool', [str, None]) or ctx.params.get('model.hlp_tool', [str, None])
        params = SimpleNamespace(get=lambda key, spec: tool if key == 'model.hlp_tool' else ctx.params.get(key, spec))
        proxy = _Context(ctx, params)
        proxy.feedback = SimpleNamespace(state={**dict(ctx.feedback.state or {}),
            'annotation.human.task_description': ctx.feedback.metadata['instruction']})
        result = await super().run(proxy)
        # The planner contract says to keep the current object-level subgoal
        # until the visual verifier confirms it.  Re-entering the planner after
        # every negative verification only asks Qwen to reproduce the same
        # instruction and stalls all simulator workers behind the reasoner.
        # Keep this system-specific and configurable so the reference Graph
        # structure and the framework verifier contract remain unchanged.
        if (
            ctx.params.get(
                'policy.reuse_subgoal_until_verified', [bool, None]
            )
            and result.get('subtask_success') is False
        ):
            result['needs_plan'] = False
            ctx.values['needs_plan'] = False
            result['reused_subgoal'] = True
        return result


@register_module('robocasa_system_terminal', kind='evaluator')
class Terminal(BenchmarkTerminal):
    async def run(self, ctx):
        result = await super().run(ctx)
        return {**result, 'needs_plan': bool(ctx.inputs.get('needs_plan', True))}
