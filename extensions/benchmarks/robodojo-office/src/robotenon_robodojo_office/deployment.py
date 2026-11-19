"""Managed process recipe for the RoboDojo official policy bridge."""

from __future__ import annotations

import sys


def policy_server(config, host: str, port: int) -> list[str]:
    """Build an argv list; all provider-specific options stay in extra_args."""

    reserved = {"--host", "--port"}
    if any(argument.split("=", 1)[0] in reserved for argument in config.extra_args):
        raise ValueError("extra_args cannot override the managed host or port")
    return [
        config.executable or sys.executable,
        "-m",
        "robotenon_robodojo_office.server",
        "--host",
        host,
        "--port",
        str(port),
        *config.extra_args,
    ]


__all__ = ["policy_server"]
