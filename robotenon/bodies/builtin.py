"""Dependency-light Body providers shipped with RoboTenon.

The providers only adapt the existing service implementations.  They do not
connect to a robot at import time: ``load_builtin_bodies`` imports this module
and the selected factory is instantiated later by runtime bootstrap.
"""

from __future__ import annotations

from typing import Any

from .registry import register_body


def _in_process_body(*, executor: Any = None, **kwargs: Any):
    if executor is None:
        raise ValueError(
            "body 'in-process' requires the runtime dependency executor=..."
        )
    from ..services.body import InProcessBodyService

    return InProcessBodyService(executor, **kwargs)


def _ipc_body(
    *,
    socket_path: str,
    timeout: float = 5.0,
    max_message_bytes: int = 8 * 1024 * 1024,
    transport: str = "uds",
    plugin_transports: tuple[str, ...] = (),
    transport_params: dict[str, Any] | None = None,
    **_: Any,
):
    from ..services.ipc import BodyServiceClient

    return BodyServiceClient(
        socket_path,
        timeout=timeout,
        max_message_bytes=max_message_bytes,
        transport=transport,
        plugin_transports=plugin_transports,
        transport_params=transport_params,
    )


register_body(
    "in-process",
    _in_process_body,
    version="1.0.0",
    kind="local",
    description="Body service running in the current process",
)
# Keep the Python-safe spelling convenient for programmatic configuration.
register_body(
    "in_process",
    _in_process_body,
    version="1.0.0",
    kind="local",
    description="Body service running in the current process",
)
register_body(
    "ipc",
    _ipc_body,
    version="1.0.0",
    kind="remote",
    description="UDS BodyService client",
)
register_body(
    "uds",
    _ipc_body,
    version="1.0.0",
    kind="remote",
    description="UDS BodyService client",
)


__all__ = ["_in_process_body", "_ipc_body"]
