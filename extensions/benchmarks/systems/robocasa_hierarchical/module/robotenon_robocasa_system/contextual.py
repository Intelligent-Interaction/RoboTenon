"""Inference-only instruction selection with bounded execution context.

The model selects the instruction without task branches or progress thresholds.
The check interval controls inference frequency, not success or recovery.
"""
import json
from collections.abc import Mapping


def advisory_prompt(task, current, history, memory):
    return (
        'You provide visual feedback to a pretrained Xiaomi robot policy. The policy '
        'already executes complete tasks from their original instructions, fresh images, '
        'and robot state. Its original task will always be preserved verbatim. Your role '
        'is to decide whether additional observation-grounded information would help it. '
        'Compare the previous and current images, and update a concise scene memory. '
        'Previous images precede the most recent executed instruction-history entry; '
        'current images show its outcome. Commands in history are not evidence of success. '
        'Distinguish visible facts from uncertainty. Select null guidance when you have '
        'no useful additional information, including when continuing the original task '
        'is appropriate. Otherwise provide a concise visual clarification or correction '
        'that helps accomplish the complete original task. Your output is supplementary '
        'feedback, not a replacement task, a termination decision, or a claim of success. '
        'Return JSON with two fields: "memory" (a concise string summarizing visual '
        'progress and uncertainty), and "guidance" (null or a nonempty string).\n'
        f'Original task: {task}\n'
        f'Current policy instruction: {current or task}\n'
        f'Previous scene memory: {memory or "none"}\n'
        f'Recent execution context: {json.dumps(history, ensure_ascii=False)}'
    )


async def plan(ctx, *, advisory=False):
    from robotenon.contracts.workspace import thaw
    from .modules import _task_text

    task = _task_text(ctx)
    tool = ctx.params.get('model.hlp_tool', [str, None])
    if not task or not tool:
        raise ValueError('contextual planner requires task instruction and HLP tool')
    source = thaw(ctx.inputs).get('stm')
    stm = dict(source) if isinstance(source, Mapping) else {}
    current = stm.get('current_subgoal')
    elapsed = int(stm.get('chunks_since_progress_check', 0))
    interval = max(1, int(ctx.params.get('policy.progress_check_interval_chunks', [int, 3])))
    history = list(stm.get('instruction_history', []))
    metadata = {}
    if not current or elapsed >= interval:
        # This node is re-entered only once the previous action chunk is spent.
        if current:
            history.append({'instruction': current, 'executed_chunks': elapsed})
        history = history[-8:]
        prompt = (
            'You supervise Xiaomi, a robot vision-language-action policy trained to '
            'execute complete natural-language manipulation tasks. It receives fresh '
            'camera images and robot state on every action prediction. Select the '
            'instruction it should execute next to accomplish the original task. '
            'You may use the original instruction verbatim, keep the current instruction, '
            'or supply a context-specific instruction. Whole-task instructions are valid; '
            'decomposition into atomic actions is not required. Use the visual evidence '
            'and execution context to make this choice. The previous images show the '
            'scene BEFORE the most recent instruction-history entry was executed; the '
            'current images show its outcome. History records commands executed, not '
            'claims that they succeeded. Preserve the original task objective. '
            'The environment determines final success; your output does not stop it. '
            'Return one JSON object containing only a nonempty string "instruction".\n'
            f'Original task: {task}\n'
            f'Current instruction: {current or "none"}\n'
            f'Recent execution context: {json.dumps(history, ensure_ascii=False)}'
        )
        if advisory:
            prompt = advisory_prompt(task, current, history, stm.get('scene_memory'))
        result = await ctx.call_tool(tool, {
            'task': task, 'prompt': prompt,
            'system_prompt': 'Ground your robot instruction in the supplied observations. Return JSON only.',
        })
        if not result.ok:
            raise RuntimeError(f'{tool} Tool failed: {result.error}')
        output = result.output
        if advisory:
            if not isinstance(output, Mapping) or 'guidance' not in output:
                raise ValueError('advisory planner requires an explicit guidance field')
            memory, guidance = output.get('memory'), output['guidance']
            if not isinstance(memory, str) or not memory.strip():
                raise ValueError('advisory planner requires nonempty scene memory')
            if guidance is not None and (not isinstance(guidance, str) or not guidance.strip()):
                raise ValueError('advisory guidance must be null or a nonempty string')
            stm['scene_memory'] = memory.strip()
            stm['guidance'] = None if guidance is None else guidance.strip()
            current = task if guidance is None else f'{task}\n{guidance.strip()}'
        else:
            instruction = output.get('instruction') if isinstance(output, Mapping) else None
            if not isinstance(instruction, str) or not instruction.strip():
                raise ValueError('contextual planner requires a nonempty instruction string')
            current = instruction.strip()
        elapsed = 0
        metadata = result.metadata
        stm['progress_checks'] = int(stm.get('progress_checks', 0)) + 1
    stm.update(goal=task, current_subgoal=current, instruction_history=history,
               chunks_since_progress_check=elapsed + 1)
    normalized = {'instruction': current}
    values = {'stm': stm, 'instruction': current, 'planner_decision': 'act',
              'planner_result': normalized, 'hlp_success': False}
    ctx.values.update(values)
    return {**values, 'plan': normalized, 'planner': metadata}
