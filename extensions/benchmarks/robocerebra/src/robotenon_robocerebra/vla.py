"""Editable VLA entry point for robocerebra native evaluation.

Implement this file to use your model; Graph/Body/metrics need no changes.
The shipped implementation produces fixed fake actions for smoke testing.
Simulator dependencies and assets must already be installed.
"""
from typing import Any

from robotenon_benchmark_support.fake_vla import FakeBatchVLA, FakeVLA


# Optional model-requested native observation settings; explicit profile params win.
OBSERVATION_PARAMS = {}


class VLA:
    def __init__(self):
        # Load your checkpoint/client here. Each worker owns one VLA instance.
        self._fake = FakeVLA(action={'actions': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})

    def start_episode(self, task_info: dict[str, Any]) -> None:
        """Reset model history. Keys: task, action_spec, observation_spec."""
        self._fake.start_episode(task_info)

    def act(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Infer one native tick: return {"actions": [...]}.

        Input is the benchmark's native observation, including numpy RGB/state
        arrays. Preserve native dimensions, units, ordering and gripper encoding
        from action_spec/native implementation. If your model returns a chunk,
        buffer it here and return one action per call. Do not step the simulator.
        """
        return self._fake.act(observation)

    def end_episode(self, result: dict[str, Any]) -> None:
        """Receive status, metrics and steps; completed also has elapsed_sec.

        Aborted episodes have status='aborted', empty metrics and steps=None.
        Success is measured by the environment, never by the VLA.
        """
        self._fake.end_episode(result)

    def close(self) -> None:
        """Release model/client resources when the worker closes."""
        self._fake.close()


def create() -> VLA:
    """Keep this factory name: both CLI and Portal configs resolve it."""
    return VLA()


# Batch mode is selected in config; the same batch interface also accepts B=1.


class BatchVLA(FakeBatchVLA):
    """Replace act_batch with one model forward pass over requests.

    Keep history/KV/action caches keyed by request['session_id']; preserve
    request_id in every response. A batch contains 1..max_batch_size items.
    open_session/close_session delimit each episode. See docs/graph-batching.md.
    """
    def __init__(self):
        super().__init__(action={'actions': [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})


def create_batch() -> BatchVLA:
    """Shared-service factory; create() remains the legacy single-session API."""
    return BatchVLA()
