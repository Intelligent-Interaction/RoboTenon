from __future__ import annotations

import argparse
import asyncio

from ..infra.metrics import MetricsDaemon


async def serve(socket_path: str, wal_path: str | None = None, *, http_host: str | None = None,
                http_port: int | None = None) -> None:
    from ..infra.ingest_server import IngestServer
    store = MetricsDaemon(wal_path)
    ingest = await IngestServer(socket_path, store).start()
    server = ingest.server
    http_server = None

    async def http_handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError):
                return
            path = request.split(b" ", 2)[1].decode("ascii", "replace")
            if path.split("?", 1)[0] == "/metrics":
                body = store.prometheus_text().encode("utf-8")
                status = b"200 OK"
                content_type = b"text/plain; version=0.0.4"
            elif path.split("?", 1)[0] == "/health":
                body = b'{"ok":true}'
                status = b"200 OK"
                content_type = b"application/json"
            else:
                body = b"not found\n"
                status = b"404 Not Found"
                content_type = b"text/plain"
            response = (b"HTTP/1.1 " + status + b"\r\nContent-Type: " + content_type
                        + b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            writer.write(response)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    if http_port is not None:
        http_server = await asyncio.start_server(http_handle, host=http_host or "127.0.0.1", port=http_port)
    try:
        servers = [server.serve_forever()]
        if http_server is not None:
            servers.append(http_server.serve_forever())
        await asyncio.gather(*servers)
    finally:
        await ingest.close()
        if http_server is not None:
            http_server.close()
            await http_server.wait_closed()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="Tenon metricsd", description="RoboTenon resident metrics daemon")
    parser.add_argument("--socket", default="/tmp/robotenon-metrics.sock")
    parser.add_argument("--wal", default="./runs/metrics.jsonl")
    parser.add_argument("--http-host", default=None, help="optional Prometheus exporter host")
    parser.add_argument("--http-port", type=int, default=None, help="optional Prometheus exporter port")
    args = parser.parse_args(argv)
    try:
        asyncio.run(serve(args.socket, args.wal, http_host=args.http_host, http_port=args.http_port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
