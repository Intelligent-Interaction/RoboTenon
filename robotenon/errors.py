from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ErrorInfo:
    """Stable, serialisable error payload used in feedback and events."""

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details or {},
        }


class RoboTenonError(Exception):
    """Base exception.  Modules should expose ErrorInfo at boundaries."""

    code = "ROBOTENON_ERROR"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.info = ErrorInfo(self.code, message, self.retryable, details)


class ContractError(RoboTenonError):
    code = "CONTRACT_ERROR"


class QueueError(RoboTenonError):
    code = "QUEUE_ERROR"


class LeaseLostError(QueueError):
    code = "LEASE_LOST"
    retryable = True


class EnvironmentError(RoboTenonError):
    code = "ENVIRONMENT_ERROR"
    retryable = True


class ReleaseError(RoboTenonError):
    code = "RELEASE_ERROR"

