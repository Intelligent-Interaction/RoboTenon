"""Stable feedback/observation contract exports.

The canonical models remain in :mod:`robotenon.schemas` for backwards
compatibility.  This module gives environment and tool integrations a small,
discoverable import path for the URI-based observation contract.
"""

from ..schemas import EnvironmentFeedback, ObservationRef

__all__ = ["EnvironmentFeedback", "ObservationRef"]
