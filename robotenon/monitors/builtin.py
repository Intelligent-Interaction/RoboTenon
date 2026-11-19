"""Small monitor plugins shipped with RoboTenon."""

from __future__ import annotations

from ..runtime.safety import max_safety_level
from ..schemas import (
    EnvironmentFeedback,
    MonitorReport,
    SafetyLevel,
    SafetySignal,
)
from .registry import register_monitor


@register_monitor(
    "feedback-safety",
    version="1.0.0",
    kind="safety",
    description="Derive a safety report from EnvironmentFeedback levels/signals.",
)
class FeedbackSafetyMonitor:
    """Default checker for safety levels already emitted by an environment.

    It intentionally does not invent a risk score.  Vendor/simulator-specific
    monitors should inspect their own state and return an explicit
    ``MonitorReport`` under a custom registry string.
    """

    def __init__(self, *, source: str = "feedback-safety") -> None:
        self.source = source

    async def check(self, feedback=None, *, request=None, context=None):
        if not isinstance(feedback, EnvironmentFeedback):
            return MonitorReport(
                session_id=None,
                compliant=True,
                safety_level=SafetyLevel.P0,
                reason="no_feedback",
                source=self.source,
            )
        levels = [feedback.safety_level]
        signals: list[SafetySignal] = []
        for item in feedback.signals:
            if item.safety_level is None:
                continue
            levels.append(item.safety_level)
            signals.append(
                SafetySignal(
                    code=f"{self.source}.{item.name}",
                    level=item.safety_level,
                    source=self.source,
                    message=(
                        str(item.value)
                        if item.value is not None and not isinstance(item.value, dict)
                        else None
                    ),
                    timestamp=item.timestamp,
                    latched=item.safety_level.rank >= SafetyLevel.P3.rank,
                    metadata=item.metadata,
                )
            )
        level = max_safety_level(*levels)
        if level.rank >= SafetyLevel.P3.rank:
            compliant, reason = False, f"safety_level_{level.value}"
        elif feedback.error:
            compliant, reason = False, "environment_feedback_error"
            level = max_safety_level(level, SafetyLevel.P2)
        else:
            compliant, reason = True, "ok"
        return MonitorReport(
            session_id=feedback.session_id,
            compliant=compliant,
            safety_level=level,
            signals=signals,
            reason=reason,
            source=self.source,
            metadata={"sequence_no": feedback.sequence_no},
        )


__all__ = ["FeedbackSafetyMonitor"]
