from __future__ import annotations

import json
from uuid import uuid4

from robotenon.infra.recorder import EpisodeRecorder
from robotenon.schemas import Action, EnvironmentFeedback


def test_episode_recorder_flushes_background_writer_at_finalize(tmp_path):
    recorder = EpisodeRecorder(tmp_path / "episode", queue_size=4)
    session_id = uuid4()
    recorder.start(session_id=session_id, task_id="task", environment_id="fake")
    recorder.record_action(Action(session_id=session_id))
    recorder.record_feedback(EnvironmentFeedback(session_id=session_id, sequence_no=0))
    manifest = recorder.finalize(status="succeeded", success=True)
    recorder.close()

    assert manifest.success is True
    actions = (tmp_path / "episode" / "actions.jsonl").read_text(encoding="utf-8").splitlines()
    feedback = (tmp_path / "episode" / "feedback.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(actions[0])["session_id"] == str(session_id)
    assert json.loads(feedback[0])["sequence_no"] == 0
    assert (tmp_path / "episode" / "manifest.json").exists()


def test_episode_recorder_rejects_records_after_close(tmp_path):
    recorder = EpisodeRecorder(tmp_path / "episode")
    session_id = uuid4()
    recorder.start(session_id=session_id, task_id="task", environment_id="fake")
    recorder.close()
    try:
        recorder.record_feedback(EnvironmentFeedback(session_id=session_id))
    except RuntimeError as exc:
        assert "closed" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("recording after close must fail")
