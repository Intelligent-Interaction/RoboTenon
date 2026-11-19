from uuid import uuid4

from robotenon.schemas import Action, ActionType, EnvironmentFeedback


def test_action_feedback_roundtrip():
    session_id = uuid4()
    action = Action(session_id=session_id, action_type=ActionType.GRIPPER, payload={"open": True})
    feedback = EnvironmentFeedback(session_id=session_id, sequence_no=1, action_id=action.action_id,
                                   action_status="succeeded", state={"gripper": "open"})
    restored = EnvironmentFeedback.model_validate_json(feedback.model_dump_json())
    assert restored.action_id == action.action_id
    assert restored.session_id == session_id
    assert restored.action_status.value == "succeeded"
