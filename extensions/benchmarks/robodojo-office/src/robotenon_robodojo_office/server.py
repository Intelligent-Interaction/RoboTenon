"""XPolicyLab WebSocket server entry for a configured RoboTenon executor."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

from .policy_bridge import RoboTenonPolicyBridge


def load_factory(reference: str):
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("executor factory must use module:callable syntax")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError(f"executor factory is not callable: {reference}")
    return factory


async def serve(args: argparse.Namespace) -> None:
    xpolicy_root = Path(args.xpolicylab_root).expanduser().resolve()
    if not (xpolicy_root / "client_server" / "ws" / "model_server.py").is_file():
        raise FileNotFoundError(f"invalid XPolicyLab root: {xpolicy_root}")
    for path in (xpolicy_root.parent, xpolicy_root):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from client_server.ws.model_server import PolicyServer, PolicyServerConfig

    config: dict[str, Any] = {}
    if args.config:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    factory = load_factory(args.executor_factory)
    executor = factory(config)
    if inspect.isawaitable(executor):
        executor = await executor
    bridge = RoboTenonPolicyBridge(executor)
    server = PolicyServer(
        bridge,
        PolicyServerConfig(host=args.host, port=args.port),
    )
    try:
        await server.serve_forever()
    finally:
        closer = getattr(executor, "close", None)
        if callable(closer):
            value = closer()
            if inspect.isawaitable(value):
                await value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xpolicylab-root", required=True)
    parser.add_argument("--executor-factory", required=True, help="module:callable")
    parser.add_argument("--config", help="JSON configuration passed to the executor factory")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    asyncio.run(serve(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
