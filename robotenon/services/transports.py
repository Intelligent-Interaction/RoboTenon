"""Stream transport plugins beneath the JSON-lines service protocol."""
from __future__ import annotations

import asyncio
import os
import stat
import socket
from pathlib import Path

from ..contracts.providers import ProviderRegistry

transport_registry = ProviderRegistry("stream transport", ("connect", "listen", "close"))
register_transport = transport_registry.register


def build_transport(endpoint, *, transport="uds", plugin_transports=(), transport_params=None):
    transport_registry.load(plugin_transports)
    return transport_registry.build(transport, params=transport_params, endpoint=str(endpoint))


@register_transport("uds")
class UnixStreamTransport:
    def __init__(self, *, endpoint):
        self.path = Path(endpoint)
        self._socket_identity = None

    async def connect(self, *, limit):
        return await asyncio.open_unix_connection(str(self.path), limit=limit)

    async def listen(self, handler, *, limit):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            mode = self.path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"IPC endpoint exists and is not a Unix socket: {self.path}")
            try:
                _, writer = await asyncio.wait_for(self.connect(limit=limit), timeout=0.2)
            except ConnectionRefusedError:
                self.path.unlink(missing_ok=True)
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError(f"IPC endpoint is already active: {self.path}")
        # Bind directly: asyncio's path-based helper can unlink an endpoint
        # created by another process between our liveness probe and bind.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.path))
            info = self.path.stat()
            self._socket_identity = (info.st_dev, info.st_ino)
            sock.setblocking(False)
            os.chmod(self.path, 0o660)
            return await asyncio.start_unix_server(handler, sock=sock, limit=limit)
        except BaseException:
            sock.close()
            await self.close()
            raise

    async def close(self):
        if self._socket_identity is None:
            return
        try:
            info = self.path.stat()
            if (info.st_dev, info.st_ino) == self._socket_identity:
                self.path.unlink()
        except FileNotFoundError:
            pass
        self._socket_identity = None
