"""Per-step recording hooks for native benchmark adapters.

Only the current step is retained here. RoboTenon's feedback/Trace owns durable
storage. A caller may supply streaming step/video sinks; no second database or
evaluation scheduler is introduced.
"""
from __future__ import annotations

from typing import Any, Callable


class EpisodeRecorder:
    def __init__(self, *, step_sink: Callable | None = None,
                 video_sink: Callable | None = None) -> None:
        self._next_step = 0
        self.step_id = -1
        self.fields: dict[str, Any] = {}
        self.step_sink = step_sink
        self.video_sink = video_sink

    @property
    def is_active(self) -> bool:
        return True

    def record_step(self, **fields: Any) -> None:
        step = int(fields.pop("step", self._next_step))
        if step < 0 or step < self.step_id:
            raise ValueError("cannot patch an expired benchmark step")
        if step != self.step_id:
            self.fields = {}
        self.step_id = step
        self._next_step = max(self._next_step, step + 1)
        self.fields.update(fields)
        if self.step_sink:
            self.step_sink(step, dict(self.fields))

    def record_video(self, frame: Any) -> None:
        if frame is not None and self.video_sink:
            self.video_sink(frame)


class NullEpisodeRecorder(EpisodeRecorder):
    @property
    def is_active(self) -> bool:
        return False
