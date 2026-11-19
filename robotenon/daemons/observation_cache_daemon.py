"""Standalone resident ObservationCache daemon.

The daemon is intentionally small: Environment owns the adapter and sends
URI-only metadata over the JSON-lines UDS, while this process owns L0/L1/L2
cache state.  A shared filesystem (or an object-store-backed custom client)
keeps media out of the RPC payload.
"""

from __future__ import annotations

import argparse
import asyncio

from ..infra.observation_cache import ObservationCache
from ..services.ipc import ObservationCacheServiceServer


async def serve(
    socket_path: str,
    root: str,
    *,
    index_path: str | None = None,
    max_records: int = 64,
    max_bytes: int = 268_435_456,
    ttl_seconds: float = 300.0,
    queue_size: int = 256,
    workers: int = 2,
    on_queue_full: str = "drop_oldest",
) -> None:
    cache = ObservationCache(
        root,
        index_path=index_path,
        max_records=max_records,
        max_bytes=max_bytes,
        ttl_seconds=ttl_seconds,
        queue_size=queue_size,
        workers=workers,
        on_queue_full=on_queue_full,
    )
    server = ObservationCacheServiceServer(socket_path, cache)
    await cache.start()
    try:
        await server.start()
        print(f"RoboTenon ObservationCache: {socket_path} -> {root}")
        await server.serve_forever()
    finally:
        await server.close()
        await cache.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="Tenon observation-cached", description="RoboTenon resident observation cache daemon")
    parser.add_argument("--socket", default="/tmp/robotenon-observation.sock")
    parser.add_argument("--root", default="./data/observations")
    parser.add_argument("--index", default=None)
    parser.add_argument("--max-records", type=int, default=64)
    parser.add_argument("--max-bytes", type=int, default=268_435_456)
    parser.add_argument("--ttl-seconds", type=float, default=300.0)
    parser.add_argument("--queue-size", type=int, default=256)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--on-queue-full",
        choices=("block", "drop_oldest", "drop_newest", "error"),
        default="drop_oldest",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(
            serve(
                args.socket,
                args.root,
                index_path=args.index,
                max_records=args.max_records,
                max_bytes=args.max_bytes,
                ttl_seconds=args.ttl_seconds,
                queue_size=args.queue_size,
                workers=args.workers,
                on_queue_full=args.on_queue_full,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

