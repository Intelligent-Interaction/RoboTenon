"""Reusable lifecycle primitives for EnvironmentAdapter implementations."""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any

from ...schemas import EnvironmentDescriptor, EnvironmentHealth, SessionSpec


class BaseEnvironmentAdapter(ABC):
    """Resident adapter with shared connection and session ownership logic.

    Providers implement ``_create_session`` and may override the protected
    connection hooks.  ``create_session`` intentionally defaults to ``open``
    so both spellings remain available to third-party callers.
    """

    descriptor: EnvironmentDescriptor

    def __init__(self) -> None:
        self._sessions: dict[Any, Any] = {}
        self._connected = False
        self._closed = False

    def describe(self) -> EnvironmentDescriptor:
        return self.descriptor

    async def connect(self) -> None:
        if self._closed:
            self._closed = False
        if self._connected:
            return
        await self._connect_impl()
        self._connected = True

    async def _connect_impl(self) -> None:
        return None

    async def open(self, spec: SessionSpec) -> Any:
        if self._closed:
            self._closed = False
        await self.connect()
        session = self._create_session(spec)
        if inspect.isawaitable(session):
            session = await session
        session_id = getattr(session, "session_id", id(session))
        self._sessions[session_id] = session
        return session

    async def create_session(self, spec: SessionSpec) -> Any:
        return await self.open(spec)

    @abstractmethod
    def _create_session(self, spec: SessionSpec) -> Any:
        raise NotImplementedError

    async def health(self) -> EnvironmentHealth:
        if self._closed:
            return EnvironmentHealth(ready=False, degraded=True, message="closed")
        if not self._connected:
            return EnvironmentHealth(
                ready=False,
                degraded=True,
                message="not_connected",
            )
        return EnvironmentHealth(ready=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None
        for session in tuple(self._sessions.values()):
            closer = getattr(session, "close", None)
            if not callable(closer):
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                # Close every session before surfacing the first error.
                if first_error is None:
                    first_error = exc
        self._sessions.clear()
        try:
            result = self._close_impl()
            if inspect.isawaitable(result):
                await result
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        self._connected = False
        if first_error is not None:
            raise first_error

    async def _close_impl(self) -> None:
        return None


__all__ = ["BaseEnvironmentAdapter"]
