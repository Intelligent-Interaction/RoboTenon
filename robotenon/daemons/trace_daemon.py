from __future__ import annotations

import argparse
import asyncio

from ..infra.trace import TraceDaemon


async def serve(socket_path: str, wal_path: str | None = None, db_path: str | None = None) -> None:
    from ..infra.ingest_server import IngestServer
    store = TraceDaemon(wal_path, db_path=db_path)
    server = IngestServer(socket_path, store)
    try:
        await server.start()
        await server.server.serve_forever()
    finally:
        await server.close()
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="Tenon traced", description="RoboTenon resident trace daemon")
    parser.add_argument("--socket", default="/tmp/robotenon-trace.sock")
    parser.add_argument("--wal", default="./runs/trace.jsonl")
    parser.add_argument("--db", default=None, help="SQLite trace index (defaults to WAL basename.sqlite3)")
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(args.socket, args.wal, args.db))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
