"""Reference contracts captured from af74484a, independent of local Git history."""
import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_qwen_managed_system import Provider, SYSTEM, ROOT, parse_output, payload
from robotenon.modules.plan import PlanModule
from robotenon.modules.verify import VerifyModule
from robotenon_robocasa_system.modules import Plan as RoboCasaPlan
from robotenon_robocasa_system.modules import Verify as RoboCasaVerify

REFERENCE = Path(__file__).parent / 'fixtures/af74484a'


def test_core_planner_verifier_and_json_tool_match_reference():
    for path, expected in json.loads((REFERENCE/'source_hashes.json').read_text()).items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest() == expected


def test_graph_control_flow_matches_reference_with_terminal_guard():
    reference = json.loads((REFERENCE/'graphs.json').read_text())['graphs']
    current = json.loads((SYSTEM/'configs/graphs.json').read_text())['graphs']
    mapping = {'robocasa_system_plan':'plan',
               'robocasa_system_action':'xiaomi_robotics_1_action',
               'robocasa_system_verify':'verify', 'robocasa_system_terminal':'robocasa365_terminal'}
    for actual, expected in zip(current, reference, strict=True):
        assert actual['id'] == expected['id']
        nodes=[]
        for original in actual['nodes']:
            node=deepcopy(original)
            if node['id']=='terminal_guard':
                assert node['next'][1]['node']=='END'
                continue
            if node.get('module') in mapping:node['module']=mapping[node['module']]
            if node['id']=='observe':node['next']=[{'node':'verify'}]
            if node['id']=='verify':node['predecessors']=['observe']
            nodes.append(node)
        assert nodes == expected['nodes']


def context(output, inputs=None):
    async def call_tool(name, arguments):
        return SimpleNamespace(ok=True, output=output, metadata={})
    params={'model.hlp_tool':'planner','task.task_id':'CloseFridge',
            'policy.planner_stop_requires_environment_success':True}
    return SimpleNamespace(inputs=inputs or {},values={},body_result=None,
        params=SimpleNamespace(get=lambda key,spec:params.get(key)),call_tool=call_tool,
        feedback=SimpleNamespace(state={'annotation.human.task_description':'Close the fridge.'},
                                 metadata={'instruction':'Close the fridge.'},success=False))


@pytest.mark.parametrize('value,expected', [(None,[]),(False,[]),('',[]),
    ('Open door',['Open door']),(['Open door'],['Open door']),([1],['1'])])
def test_planner_fields_are_normalized_by_module(value,expected):
    parsed=parse_output(json.dumps({'decision':'ACT','completed':value}), 'planner')
    result=asyncio.run(PlanModule().run(context(parsed)))
    assert result['plan']['completed']==expected
    assert result['plan']['subgoal']=='Close the fridge.'
    assert result['plan']['decision']=='act'
    assert result['plan']['remaining']==['Close the fridge.']


def test_empty_planner_object_and_unconfirmed_stop_match_reference():
    result=asyncio.run(PlanModule().run(context(parse_output('{}','planner'))))
    assert result['plan']['decision']=='act'
    result=asyncio.run(PlanModule().run(context({'decision':'stop','remaining':[]},
        {'stm':{'recent_verifier':{'subtask_success':True}}})))
    assert result['plan']['decision']=='act'
    assert not result['hlp_success']


def test_verifier_still_rejects_non_boolean_at_module_boundary():
    output=parse_output('{"subtask_success":"false"}','verifier')
    with pytest.raises(ValueError,match='JSON boolean'):
        asyncio.run(VerifyModule().run(context(output)))


@pytest.mark.parametrize(
    "verified, expected_needs_plan",
    [(False, False), (True, True)],
)
def test_robocasa_reuses_subgoal_until_verifier_confirms_it(
    verified, expected_needs_plan
):
    async def call_tool(name, arguments):
        return SimpleNamespace(
            ok=True,
            output={
                "subtask_success": verified,
                "physical_belief_update": {},
            },
            metadata={},
        )

    values = {}
    params = {
        "model.verify_tool": "verifier",
        "model.hlp_tool": "planner",
        "task.task_id": "CloseFridge",
        "policy.reuse_subgoal_until_verified": True,
    }
    ctx = SimpleNamespace(
        inputs={"instruction": "Close the fridge.", "needs_plan": True},
        values=values,
        body_result=SimpleNamespace(executed_actions=16),
        params=SimpleNamespace(get=lambda key, spec: params.get(key)),
        call_tool=call_tool,
        feedback=SimpleNamespace(
            state={},
            metadata={"instruction": "Close the fridge."},
            success=False,
        ),
    )
    result = asyncio.run(RoboCasaVerify().run(ctx))
    assert result["needs_plan"] is expected_needs_plan
    assert values["needs_plan"] is expected_needs_plan
    assert result.get("reused_subgoal", False) is (not verified)


@pytest.mark.parametrize("needs_plan", [False, True])
def test_robocasa_can_disable_verifier_and_preserve_chunk_routing(needs_plan):
    async def unexpected_tool(*args):
        raise AssertionError("disabled verifier must not call its model Tool")

    values = {}
    params = {
        "model.verify_tool": "verifier",
        "model.hlp_tool": "planner",
        "task.task_id": "CloseFridge",
        "policy.verify_enabled": False,
    }
    ctx = SimpleNamespace(
        inputs={
            "instruction": "Close the fridge.",
            "stm": {"goal": "close"},
            "needs_plan": needs_plan,
        },
        values=values,
        body_result=SimpleNamespace(executed_actions=16),
        params=SimpleNamespace(get=lambda key, spec: params.get(key)),
        call_tool=unexpected_tool,
        feedback=SimpleNamespace(
            state={}, metadata={"instruction": "Close the fridge."}, success=False
        ),
    )
    result = asyncio.run(RoboCasaVerify().run(ctx))
    assert result == {
        "stm": {"goal": "close"},
        "needs_plan": needs_plan,
        "verification": None,
        "verifier_result": None,
        "subtask_success": None,
        "skipped": True,
        "reason": "verifier_disabled",
    }
    assert values == result


def _system_plan_context(outputs, stm=None, interval=3):
    calls = []

    async def call_tool(name, arguments):
        calls.append((name, arguments))
        output = outputs[min(len(calls) - 1, len(outputs) - 1)]
        return SimpleNamespace(ok=True, output=output, metadata={"batch": 1})

    params = {
        "model.hlp_tool": "planner",
        "task.task_id": "generic-task",
        "policy.progress_check_interval_chunks": interval,
    }
    ctx = SimpleNamespace(
        inputs={"stm": stm or {}},
        values={},
        params=SimpleNamespace(get=lambda key, spec: params.get(key, spec[1])),
        call_tool=call_tool,
        feedback=SimpleNamespace(
            state={"annotation.human.task_description": "Put the object in its target."},
            metadata={"instruction": "Put the object in its target."},
            success=False,
        ),
    )
    return ctx, calls


def test_generic_system_planner_persists_exact_subgoal_between_visual_checks():
    ctx, calls = _system_plan_context([
        {"status": "advance", "subgoal": "Pick up the object"},
    ])
    first = asyncio.run(RoboCasaPlan().run(ctx))
    assert first["instruction"] == "Pick up the object"
    assert first["planner_result"] == {
        "status": "advance", "subgoal": "Pick up the object"
    }
    assert len(calls) == 1

    stm = first["stm"]
    for expected_age in (2, 3):
        ctx, skipped_calls = _system_plan_context([], stm)
        result = asyncio.run(RoboCasaPlan().run(ctx))
        assert result["instruction"] == "Pick up the object"
        assert result["planner_result"]["status"] == "continue"
        assert result["stm"]["subgoal_age_chunks"] == expected_age
        assert skipped_calls == []
        stm = result["stm"]

    ctx, check_calls = _system_plan_context([
        {"status": "continue", "subgoal": "Use different wording"},
    ], stm)
    checked = asyncio.run(RoboCasaPlan().run(ctx))
    assert len(check_calls) == 1
    assert checked["instruction"] == "Pick up the object"
    assert checked["stm"]["subgoal_age_chunks"] == 4
    assert checked["stm"]["chunks_since_progress_check"] == 1


@pytest.mark.parametrize("status", ["advance", "recover"])
def test_generic_system_planner_only_replaces_subgoal_on_explicit_transition(status):
    stm = {
        "current_subgoal": "Pick up the object",
        "subgoal_age_chunks": 6,
        "chunks_since_progress_check": 3,
    }
    ctx, calls = _system_plan_context([
        {"status": status, "subgoal": "Place the object in the target"},
    ], stm)
    result = asyncio.run(RoboCasaPlan().run(ctx))
    assert len(calls) == 1
    assert result["instruction"] == "Place the object in the target"
    assert result["stm"]["subgoal_age_chunks"] == 1
    assert result["planner_decision"] == "act"
    assert result["hlp_success"] is False


def test_syntax_retry_preserves_prompt_budget_and_records_raw_failure(tmp_path):
    provider=Provider.__new__(Provider)
    provider.sessions={'a':{}};provider.counts={'planner':0,'verifier':0,'forward_batches':0,'response_retries':0}
    provider.last_results=[];provider.max_response_retries=2;provider.max_new_tokens=1024;provider.metrics_dir=tmp_path
    calls=[]
    def generate(messages,budget):
        calls.append((messages,budget))
        return [('not JSON',False)] if len(calls)==1 else [('{"completed":null}',False)]
    provider._generate=generate
    output=provider.infer_batch([{'session_id':'a','request_id':'r','observation':payload()}])
    assert output[0]['output']=={'completed':None}
    assert calls[0]==calls[1]
    record=json.loads(next(tmp_path.glob('*.json')).read_text())
    assert record['last_failures'][0]['raw_output']=='not JSON'


def test_truncated_json_is_never_accepted_and_budget_caps_at_four_times(tmp_path):
    provider=Provider.__new__(Provider)
    provider.sessions={'a':{}};provider.counts={'planner':0,'verifier':0,'forward_batches':0,'response_retries':0}
    provider.last_results=[];provider.max_response_retries=2;provider.max_new_tokens=1024;provider.metrics_dir=tmp_path
    budgets=[]
    def generate(messages,budget):
        assert len(messages[0])==2
        budgets.append(budget)
        return [('{"completed":[]}',True)]
    provider._generate=generate
    with pytest.raises(RuntimeError,match='token limit'):
        provider.infer_batch([{'session_id':'a','request_id':'r','observation':payload()}])
    assert budgets==[1024,2048,4096]
    assert provider.counts['planner']==0
    assert len(json.loads(next(tmp_path.glob('*.json')).read_text())['last_failures'])==3


def test_contextual_planner_preserves_whole_task_and_records_executed_context():
    from robotenon_robocasa_system.contextual import plan
    instruction = 'Move the object to the target and finish the requested arrangement without changing which object or target the original task specifies.'
    ctx, calls = _system_plan_context([{'instruction': instruction}])
    first = asyncio.run(plan(ctx))
    assert first['instruction'] == instruction
    assert first['stm']['instruction_history'] == []
    stm = first['stm']
    for _ in range(2):
        ctx, calls = _system_plan_context([], stm)
        result = asyncio.run(plan(ctx))
        assert not calls
        assert result['instruction'] == instruction
        stm = result['stm']
    ctx, calls = _system_plan_context([{'instruction': 'Put the object in its target.'}], stm)
    result = asyncio.run(plan(ctx))
    assert result['instruction'] == 'Put the object in its target.'
    assert result['stm']['instruction_history'] == [{'instruction': instruction, 'executed_chunks': 3}]
    assert instruction in calls[0][1]['prompt']
    assert result['hlp_success'] is False
    assert stm['instruction_history'] == []  # no aliasing with prior Graph state


@pytest.mark.parametrize('output', [{}, {'instruction': False}, {'instruction': ''}])
def test_contextual_planner_does_not_silently_replace_invalid_model_output(output):
    from robotenon_robocasa_system.contextual import plan
    ctx, _ = _system_plan_context([output])
    with pytest.raises(ValueError, match='nonempty instruction'):
        asyncio.run(plan(ctx))


def test_advisory_guidance_retains_original_goal_and_can_be_withdrawn():
    from robotenon_robocasa_system.contextual import plan
    ctx, calls = _system_plan_context([{'memory': 'The target is partly obscured.', 'guidance': 'The target is behind the object.'}])
    first = asyncio.run(plan(ctx, advisory=True))
    original = first['stm']['goal']
    assert first['instruction'] == original + '\nThe target is behind the object.'
    stm = first['stm']
    for _ in range(2):
        ctx, calls = _system_plan_context([], stm)
        result = asyncio.run(plan(ctx, advisory=True))
        assert not calls
        assert result['instruction'] == first['instruction']
        stm = result['stm']
    ctx, calls = _system_plan_context([{'memory': 'The target is visible now.', 'guidance': None}], stm)
    result = asyncio.run(plan(ctx, advisory=True))
    assert result['instruction'] == original
    assert 'The target is partly obscured.' in calls[0][1]['prompt']
    assert result['stm']['guidance'] is None
    assert stm['scene_memory'] == 'The target is partly obscured.'
    assert not result['hlp_success']


@pytest.mark.parametrize('output', [{}, {'memory': 'visible'}, {'memory': '', 'guidance': None},
    {'memory': 'visible', 'guidance': False}, {'memory': 'visible', 'guidance': ''}])
def test_advisory_malformed_responses_are_not_silently_baseline(output):
    from robotenon_robocasa_system.contextual import plan
    ctx, _ = _system_plan_context([output])
    with pytest.raises(ValueError, match='advisory'):
        asyncio.run(plan(ctx, advisory=True))
