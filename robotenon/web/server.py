from __future__ import annotations

import argparse
import errno
import gzip
import json
import mimetypes
import os
from html import escape as html_escape
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .grafana import DEFAULT_GRAFANA_URL, ensure_grafana
from .experiments import ExperimentManager
from ..infra.trace_query import QueryBudgetExceeded
from ..infra.history import page_window
from .access import PortalAccess
from .page import portal_assets, render_page
from .query import (
    MAX_OBSERVATION_HISTORY_RESULTS,
    MAX_OBSERVATION_HISTORY_WINDOW,
    PortalData,
)
from .http_server import BoundedHTTPServer
from ..task_adapters.result_index import IndexRecoveryPending

HTML = render_page()


@lru_cache(maxsize=8)
def _compressed_asset(payload: bytes) -> bytes:
    return gzip.compress(payload, compresslevel=6, mtime=0)


class PortalHandler(BaseHTTPRequestHandler):
    data: PortalData
    experiments: ExperimentManager | None = None
    grafana_url: str | None = None
    access: PortalAccess

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _check_access(self, *, write=False, control=False):
        error = self.access.check(self.headers, write=write, control=control)
        if error is not None:
            status, message = error
            self._json({"error": message}, status=status)
            return False
        return True

    def _request_source(self) -> dict[str, object]:
        return {
            "type": "portal_http",
            "remote_address": self.client_address[0] if self.client_address else None,
            "forwarded_for": self.headers.get("X-Forwarded-For"),
            "user_agent": self.headers.get("User-Agent"),
            "method": self.command,
            "path": urlparse(self.path).path,
            # Access checks run before this snapshot.  A configured token means
            # the request supplied a credential that was actually accepted.
            "authenticated": self.access.token is not None,
        }

    def log_message(self, format: str, *args):
        return

    def _send(
        self,
        payload: bytes,
        *,
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        cache_control: str = "no-store",
    ) -> None:
        compressed = False
        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "").lower()
        compressible = content_type.startswith(
            "application/json"
        ) or content_type.startswith("text/")
        if accepts_gzip and compressible and len(payload) >= 1024:
            candidate = (
                _compressed_asset(payload)
                if "immutable" in cache_control
                else gzip.compress(payload, compresslevel=6, mtime=0)
            )
            if len(candidate) < len(payload):
                payload = candidate
                compressed = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # Explicit framing is important for large trajectory JSON responses.
        # Without it, the browser must infer the end from the SSH-forwarded
        # connection closing; an interrupted tunnel then surfaces as a JSON
        # parser error such as "Unterminated string".
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Referrer-Policy", "no-referrer")
        if status == 401:
            self.send_header(
                "WWW-Authenticate", 'Basic realm="RoboTenon Portal", charset="UTF-8"'
            )
        self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def _json(self, value: object, *, status: int = 200) -> None:
        chunks, size = [], 0
        for chunk in json.JSONEncoder(ensure_ascii=False, default=str).iterencode(
            value
        ):
            data = chunk.encode("utf-8")
            size += len(data)
            if size > 4 * 1024 * 1024:
                return self._send(
                    b'{"error":"response exceeds 4 MiB; reduce limit or narrow the time window"}',
                    status=413,
                )
            chunks.append(data)
        self._send(b"".join(chunks), status=status)

    def _send_file(self, path: Path) -> None:
        """Serve a validated local artifact with basic HTTP range support."""

        try:
            size = path.stat().st_size
        except OSError:
            return self._json({"error": "media not found"}, status=404)
        start, end = 0, max(size - 1, 0)
        status = 200
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes=") and size:
            try:
                value = range_header[6:].split(",", 1)[0]
                first, last = value.split("-", 1)
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    length = int(last)
                    start = max(size - length, 0)
                    end = size - 1
                if start < 0 or start >= size or end < start:
                    raise ValueError
                end = min(end, size - 1)
                status = 206
            except (TypeError, ValueError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        length = max(end - start + 1, 0)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", "inline")
        self.end_headers()
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def _admitted(self, handler):
        path = urlparse(self.path).path
        control = (self.command == "GET" and path == "/api/health") or (
            self.command == "POST"
            and (
                path == "/api/system/shutdown"
                or (path.startswith("/api/experiments/") and path.endswith("/stop"))
            )
        )
        kind = "controls" if control else "queries"
        if not self.server.acquire(kind):
            return self._unavailable("Portal query capacity is occupied; retry shortly")
        try:
            return handler()
        finally:
            self.server.release(kind)

    def _unavailable(self, message):
        payload = json.dumps({"error": message, "retryable": True}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Retry-After", "1")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(payload)

    def do_GET(self):
        return self._admitted(self._do_GET)

    def do_POST(self):
        return self._admitted(self._do_POST)

    def _do_GET(self):
        if not self._check_access():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path.startswith("/assets/"):
            asset = portal_assets().get(parsed.path)
            if asset is None:
                return self._json({"error": "asset not found"}, status=404)
            return self._send(
                asset[0],
                content_type=asset[1],
                cache_control="public, max-age=31536000, immutable",
            )
        if parsed.path == "/":
            if self.grafana_url:
                src = html_escape(self.grafana_url, quote=True)
                grafana = (
                    '<div class="grafana-frame-shell"><a class="grafana-expand" href="'
                    + src
                    + '" target="_blank" rel="noopener noreferrer" title="在新页面打开 Grafana" aria-label="在新页面打开 Grafana">⤢</a><div class="grafana-only"><iframe data-src="'
                    + src
                    + '" title="RoboTenon Grafana dashboard" loading="lazy" scrolling="yes"></iframe></div></div>'
                )
            else:
                grafana = '<div class="grafana-frame-shell"><div class="grafana-only"><span class="muted">未配置 Grafana。</span></div></div>'
            return self._send(
                HTML.replace(
                    '<div id="grafana"></div>',
                    '<div id="grafana">' + grafana + "</div>",
                ).encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
        try:
            limit = min(max(int(query.get("limit", ["200"])[0]), 1), 10000)

            def history_page():
                page_limit, offset, since, until = page_window(
                    query.get("limit", ["100"])[0],
                    query.get("offset", ["0"])[0],
                    query.get("since", [None])[0],
                    query.get("until", [None])[0],
                )
                return {
                    "limit": page_limit,
                    "offset": offset,
                    "since": since,
                    "until": until,
                }

            if parsed.path == "/api/runs":
                runs = self.data.runs(limit=limit)
                return self._json(
                    {
                        "runs": runs,
                        "task_item_count": self.data.task_item_count(limit=10000),
                    }
                )
            if parsed.path == "/api/configs":
                if self.experiments is None:
                    return self._json(
                        {"configs": [], "error": "experiment submission is disabled"},
                        status=503,
                    )
                return self._json({"configs": self.experiments.list_configs()})
            if parsed.path.startswith("/api/configs/"):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                config_id = unquote(parsed.path[len("/api/configs/") :])
                return self._json(self.experiments.get_config_document(config_id))
            if parsed.path == "/api/experiments":
                if self.experiments is None:
                    return self._json(
                        {
                            "experiments": [],
                            "error": "experiment submission is disabled",
                        },
                        status=503,
                    )
                return self._json(
                    self.experiments.jobs_page(
                        search=query.get("search", [""])[0][:200],
                        cursor=query.get("cursor", [None])[0],
                        **history_page(),
                    )
                )
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/traces"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[len("/api/experiments/") : -len("/traces")].strip(
                    "/"
                )
                reader = self.experiments.job_trace_query(job_id)
                if reader is None:
                    return self._json({"traces": [], "next_offset": None})
                page = reader.episode_page(
                    run_id=job_id,
                    episode_id=query.get("episode_id", [None])[0],
                    attempt_id=query.get("attempt_id", [None])[0],
                    **history_page(),
                )
                rows = page.pop("rows")
                for row in rows:
                    row["attributes"] = json.loads(row.pop("attributes_json"))
                return self._json({"traces": rows, **page})
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/fork"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[len("/api/experiments/") : -len("/fork")].strip(
                    "/"
                )
                if not job_id:
                    return self._json({"error": "job_id is required"}, status=400)
                return self._json(self.experiments.fork_draft(job_id))
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/episodes"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[
                    len("/api/experiments/") : -len("/episodes")
                ].strip("/")
                if not job_id or self.experiments.get_job(job_id) is None:
                    return self._json({"error": "experiment not found"}, status=404)
                include_runtime = query.get("include_runtime", ["0"])[0].lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                return self._json(
                    self.experiments.episodes_page(
                        job_id, include_runtime=include_runtime, **history_page()
                    )
                )
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/node-detail"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[
                    len("/api/experiments/") : -len("/node-detail")
                ].strip("/")
                path = json.loads(query.get("path", ["null"])[0])
                if not isinstance(path, list):
                    return self._json(
                        {"error": "node execution path is required"}, status=400
                    )
                detail = self.experiments.node_detail(
                    job_id,
                    adapter_id=str(query.get("adapter_id", [""])[0]),
                    global_index=int(query.get("global_index", ["-1"])[0]),
                    path=path,
                )
                return self._json(
                    detail if detail is not None else {"error": "node not found"},
                    status=200 if detail is not None else 404,
                )
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/trajectory"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[
                    len("/api/experiments/") : -len("/trajectory")
                ].strip("/")
                adapter_id = query.get("adapter_id", [None])[0]
                index_value = query.get("global_index", query.get("index", [None]))[0]
                if not job_id or not adapter_id or index_value in {None, ""}:
                    return self._json(
                        {"error": "job_id, adapter_id and global_index are required"},
                        status=400,
                    )
                view = str(query.get("view", ["full"])[0]).lower()
                detail = self.experiments.episode_detail(
                    job_id,
                    adapter_id=str(adapter_id),
                    global_index=int(index_value),
                    include_runtime=view != "summary",
                    include_timeline=view != "summary",
                )
                if detail is None:
                    return self._json({"error": "trajectory not found"}, status=404)
                return self._json(detail)
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/media"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[len("/api/experiments/") : -len("/media")].strip(
                    "/"
                )
                adapter_id = query.get("adapter_id", [None])[0]
                index_value = query.get("global_index", query.get("index", [None]))[0]
                if not job_id or not adapter_id or index_value in {None, ""}:
                    return self._json(
                        {"error": "job_id, adapter_id and global_index are required"},
                        status=400,
                    )
                if query.get("kind", ["video"])[0] != "video":
                    return self._json({"error": "unsupported media kind"}, status=400)
                path = self.experiments.episode_video_path(
                    job_id,
                    adapter_id=str(adapter_id),
                    global_index=int(index_value),
                )
                if path is None:
                    return self._json({"error": "video not found"}, status=404)
                return self._send_file(path)
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/observation"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[
                    len("/api/experiments/") : -len("/observation")
                ].strip("/")
                adapter_id = query.get("adapter_id", [None])[0]
                index_value = query.get("global_index", query.get("index", [None]))[0]
                uri = query.get("uri", [None])[0]
                artifact_id = query.get("artifact_id", [None])[0]
                storage_path = query.get("storage_path", [None])[0]
                output_format = query.get("format", [""])[0]
                if not job_id or not adapter_id or index_value in {None, ""} or not uri:
                    return self._json(
                        {
                            "error": "job_id, adapter_id, global_index and uri are required"
                        },
                        status=400,
                    )
                path = self.experiments.episode_observation_path(
                    job_id,
                    adapter_id=str(adapter_id),
                    global_index=int(index_value),
                    uri=str(uri),
                    artifact_id=str(artifact_id) if artifact_id else None,
                    storage_path=str(storage_path) if storage_path else None,
                )
                if path is None:
                    return self._json(
                        {"error": "observation image not found"}, status=404
                    )
                image = self.data.image_from_path(
                    path,
                    encoding="npy" if path.suffix.lower() == ".npy" else "",
                    output_format=str(output_format or ""),
                )
                if image is None:
                    return self._json(
                        {"error": "observation image not found"}, status=404
                    )
                return self._send(
                    image[0],
                    content_type=image[1],
                    cache_control="public, max-age=3600",
                )
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/graph"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[len("/api/experiments/") : -len("/graph")].strip(
                    "/"
                )
                if not job_id or self.experiments.get_job(job_id) is None:
                    return self._json({"error": "experiment not found"}, status=404)
                return self._json(self.experiments.graph_snapshot(job_id))
            if parsed.path.startswith("/api/experiments/") and parsed.path.endswith(
                "/terminal"
            ):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path[
                    len("/api/experiments/") : -len("/terminal")
                ].strip("/")
                offset_value = query.get("offset", [None])[0]
                offset = None if offset_value in {None, ""} else int(offset_value)
                chunk = self.experiments.terminal_output(
                    job_id,
                    offset=offset,
                    limit=int(query.get("limit", [str(64 * 1024)])[0]),
                )
                if chunk is None:
                    return self._json({"error": "experiment not found"}, status=404)
                return self._json(chunk)
            if parsed.path.startswith("/api/experiments/"):
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = parsed.path.rsplit("/", 1)[-1]
                job = self.experiments.get_job(job_id)
                if job is None:
                    return self._json({"error": "experiment not found"}, status=404)
                return self._json(job)
            if parsed.path == "/api/task-items":
                return self._json({"task_items": self.data.task_items(limit=limit)})
            if parsed.path == "/api/metrics":
                samples = self.data.metrics.snapshot(limit=limit)
                return self._json(
                    {
                        "names": self.data.metrics.names(),
                        "samples": [x.model_dump(mode="json") for x in samples],
                        "prometheus_text": self.data.metrics.prometheus_text(),
                    }
                )
            if parsed.path == "/api/traces":
                run_id, episode_id = (
                    query.get("run_id", [None])[0],
                    query.get("episode_id", [None])[0],
                )
                if bool(run_id) != bool(episode_id):
                    return self._json(
                        {"error": "run_id and episode_id are required together"},
                        status=400,
                    )
                page = self.data.traces.episode_page(
                    run_id=run_id,
                    episode_id=episode_id,
                    attempt_id=query.get("attempt_id", [None])[0],
                    **history_page(),
                )
                rows = page.pop("rows")
                if not run_id:
                    rows.reverse()
                    for row in rows:
                        row["attributes"] = json.loads(row.pop("attributes_json"))
                return self._json({"traces": rows, **page})
            if parsed.path == "/api/observations/latest":
                session_id = query.get("session_id", [None])[0]
                if not session_id:
                    return self._json({"error": "session_id is required"}, status=400)
                epoch_value = query.get("epoch", [None])[0]
                epoch = int(epoch_value) if epoch_value not in {None, ""} else None
                return self._json(
                    {"feedback": self.data.observation_latest(session_id, epoch=epoch)}
                )
            if parsed.path == "/api/observations/history":
                session_id = query.get("session_id", [None])[0]
                if not session_id:
                    return self._json({"error": "session_id is required"}, status=400)
                epoch_value = query.get("epoch", [None])[0]
                epoch = int(epoch_value) if epoch_value not in {None, ""} else None
                length = int(query.get("length", ["4"])[0])
                interval = int(query.get("interval", ["1"])[0])
                if length <= 0 or interval <= 0:
                    return self._json(
                        {"error": "length and interval must be positive"}, status=400
                    )
                if length > MAX_OBSERVATION_HISTORY_RESULTS:
                    return self._json(
                        {
                            "error": f"length exceeds the observation history result limit of {MAX_OBSERVATION_HISTORY_RESULTS}"
                        },
                        status=400,
                    )
                if length * interval > MAX_OBSERVATION_HISTORY_WINDOW:
                    return self._json(
                        {
                            "error": f"length * interval exceeds the observation history limit of {MAX_OBSERVATION_HISTORY_WINDOW} records"
                        },
                        status=400,
                    )
                return self._json(
                    {
                        "feedback": self.data.observation_history(
                            session_id, epoch=epoch, length=length, interval=interval
                        )
                    }
                )
            if parsed.path == "/api/observations/metadata":
                uri = query.get("uri", [None])[0]
                output_format = query.get("format", [""])[0]
                if not uri:
                    return self._json({"error": "uri is required"}, status=400)
                return self._json({"metadata": self.data.observation_metadata(uri)})
            if parsed.path == "/api/observations/image":
                output_format = query.get("format", [""])[0]
                uri = query.get("uri", [None])[0]
                if not uri:
                    return self._json({"error": "uri is required"}, status=400)
                image = self.data.observation_image(
                    uri, output_format=str(output_format or "")
                )
                if image is None:
                    return self._json(
                        {"error": "observation image not found"}, status=404
                    )
                # Observation URIs are content-addressed cache artifacts.  They
                # are immutable once written, so let the browser reuse them
                # across playback renders and repeated node selections.
                return self._send(
                    image[0],
                    content_type=image[1],
                    cache_control="public, max-age=3600, immutable",
                )
            if parsed.path == "/api/trace/schema":
                return self._json(
                    {
                        "rows": self.data.traces.sql(
                            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('table', 'index', 'view') ORDER BY type, name"
                        )
                    }
                )
            if parsed.path == "/api/health":
                return self._json(
                    {
                        "ok": True,
                        "metrics_wal": str(
                            getattr(getattr(self.data, "metrics", None), "wal_path", "")
                        ),
                        "trace_db": str(getattr(self.data.traces, "db_path", "")),
                        "run_root": str(getattr(self.data, "run_root", "")),
                        "admission": self.server.admission_snapshot(),
                        "retention": self.experiments.retention_report
                        if self.experiments
                        else None,
                    }
                )
        except IndexRecoveryPending as exc:
            return self._unavailable(str(exc))
        except QueryBudgetExceeded as exc:
            return self._json({"error": str(exc)}, status=413)
        except TimeoutError as exc:
            return self._json({"error": str(exc)}, status=504)
        except ValueError as exc:
            return self._json({"error": str(exc)}, status=400)
        except Exception as exc:
            return self._json({"error": str(exc)}, status=500)
        return self._json({"error": "not found"}, status=404)

    def _do_POST(self):
        path = urlparse(self.path).path
        is_fork = path.startswith("/api/experiments/") and path.endswith("/fork")
        is_stop = path.startswith("/api/experiments/") and path.endswith("/stop")
        is_rerun = path.startswith("/api/experiments/") and path.endswith("/rerun")
        is_shutdown = path == "/api/system/shutdown"
        if path not in {
            "/api/trace/sql",
            "/api/experiments",
            "/api/preflight",
        } and not (is_fork or is_stop or is_rerun or is_shutdown):
            return self._json({"error": "not found"}, status=404)
        if not self._check_access(write=True, control=path != "/api/trace/sql"):
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                return self._json(
                    {"error": "Transfer-Encoding is not supported"}, status=400
                )
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024 * 1024:
                return self._json(
                    {"error": "request body exceeds the 1 MiB limit"}, status=413
                )
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                return self._json(
                    {"error": "request body must be a JSON object"}, status=400
                )
            if path == "/api/trace/sql":
                reader = self.data.traces
                if body.get("job_id") and self.experiments is not None:
                    reader = self.experiments.job_trace_query(str(body["job_id"]))
                if reader is None:
                    return self._json(
                        {"rows": [], "next_offset": None, "truncated": False}
                    )
                page = reader.sql_page(
                    str(body.get("query", "")),
                    tuple(body.get("params", [])),
                    limit=body.get("limit", 1000),
                    offset=int(body.get("offset", 0)),
                )
                self._json(page)
                return
            if is_fork:
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                job_id = path[len("/api/experiments/") : -len("/fork")].strip("/")
                if not job_id:
                    return self._json({"error": "job_id is required"}, status=400)
                # Fork is intentionally a draft operation.  The UI must let
                # the user edit and confirm the copied config before the
                # ordinary /api/experiments POST starts a new process.
                return self._json(self.experiments.fork_draft(job_id))
            if is_stop or is_rerun:
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                suffix = "/stop" if is_stop else "/rerun"
                job_id = path[len("/api/experiments/") : -len(suffix)].strip("/")
                if not job_id:
                    return self._json({"error": "job_id is required"}, status=400)
                if "tasks" in body and (
                    not isinstance(body["tasks"], list) or not body["tasks"]
                ):
                    return self._json(
                        {"error": "tasks must be a non-empty list"}, status=400
                    )
                adapter_id = body.get("adapter_id")
                if adapter_id is not None and not isinstance(adapter_id, str):
                    return self._json(
                        {"error": "adapter_id must be a string"}, status=400
                    )
                index_value = body.get("global_index")
                global_index = None
                if index_value not in {None, ""}:
                    try:
                        global_index = int(index_value)
                    except (TypeError, ValueError):
                        return self._json(
                            {"error": "global_index must be an integer"}, status=400
                        )
                default_reason = "user_stop" if is_stop else "user_rerun"
                reason = body.get("reason", default_reason)
                if not isinstance(reason, str) or not reason.strip():
                    return self._json(
                        {"error": "reason must be a non-empty string"}, status=400
                    )
                source = self._request_source()
                if is_stop:
                    result = self.experiments.stop_job(
                        job_id,
                        adapter_id=adapter_id,
                        global_index=global_index,
                        reason=reason.strip(),
                        wait=False,
                        source=source,
                        **({"tasks": body["tasks"]} if "tasks" in body else {}),
                    )
                    return self._json(
                        result, status=202 if result.get("pending") else 200
                    )
                result = self.experiments.rerun_job(
                    job_id,
                    adapter_id=adapter_id,
                    global_index=global_index,
                    reason=reason.strip(),
                    source=source,
                    **({"tasks": body["tasks"]} if "tasks" in body else {}),
                )
                return self._json(result, status=202)
            if is_shutdown:
                if self.experiments is None:
                    return self._json(
                        {"error": "experiment submission is disabled"}, status=503
                    )
                reason = body.get("reason", "global_shutdown")
                if not isinstance(reason, str) or not reason.strip():
                    return self._json(
                        {"error": "reason must be a non-empty string"}, status=400
                    )
                result = self.experiments.shutdown_all(
                    reason=reason.strip(), wait=False, source=self._request_source()
                )
                return self._json(result, status=202 if result.get("pending") else 200)
            if self.experiments is None:
                return self._json(
                    {"error": "experiment submission is disabled"}, status=503
                )
            config = body.get("config")
            if not isinstance(config, str) or not config.strip():
                return self._json({"error": "config is required"}, status=400)
            save_videos = body.get("save_videos")
            save_traces = body.get("save_traces")
            forked_from = body.get("forked_from")
            overrides = body.get("overrides", {})
            if overrides is None:
                overrides = {}
            if not isinstance(overrides, dict):
                return self._json(
                    {"error": "overrides must be a JSON object"}, status=400
                )
            if save_videos is not None and not isinstance(save_videos, bool):
                return self._json({"error": "save_videos must be boolean"}, status=400)
            if save_traces is not None and not isinstance(save_traces, bool):
                return self._json({"error": "save_traces must be boolean"}, status=400)
            if forked_from is not None and (
                not isinstance(forked_from, str) or not forked_from.strip()
            ):
                return self._json(
                    {"error": "forked_from must be a non-empty string"}, status=400
                )
            if path == "/api/preflight":
                return self._json(self.experiments.preflight(config, overrides))
            job = self.experiments.submit(
                config,
                overrides=overrides,
                save_videos=save_videos,
                save_traces=save_traces,
                forked_from=forked_from,
            )
            self._json(job, status=202)
        except KeyError as exc:
            self._json({"error": f"not found: {exc}"}, status=404)
        except IndexRecoveryPending as exc:
            self._unavailable(str(exc))
        except QueryBudgetExceeded as exc:
            self._json({"error": str(exc)}, status=413)
        except TimeoutError as exc:
            self._json({"error": str(exc)}, status=504)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=400)
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, status=400)
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)


def build_server(
    host: str,
    port: int,
    data: PortalData,
    *,
    grafana_url: str | None = None,
    experiment_manager: ExperimentManager | None = None,
    control_token: str | None = None,
    allowed_hosts: tuple[str, ...] = (),
    loopback_published: bool = False,
    max_connections: int = 32,
    max_queries: int = 8,
    control_slots: int = 4,
) -> ThreadingHTTPServer:
    handler = type("RoboTenonPortalHandler", (PortalHandler,), {})
    handler.access = PortalAccess(
        host,
        control=experiment_manager is not None,
        token=control_token,
        allowed_hosts=allowed_hosts,
        loopback_published=loopback_published,
    )
    handler.data = data
    handler.experiments = experiment_manager
    if experiment_manager is not None:
        experiment_manager.trace_query = data.traces
    # Keep the local development path zero-configuration.  Compose exposes
    # Grafana on 127.0.0.1:3000; callers can override this for a remote host.
    handler.grafana_url = (
        grafana_url or os.getenv("ROBOTENON_GRAFANA_URL") or DEFAULT_GRAFANA_URL
    )
    server = BoundedHTTPServer(
        (host, port),
        handler,
        max_connections=max_connections,
        max_queries=max_queries,
        control_slots=control_slots,
    )
    server.control_token = handler.access.token
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="Tenon web", description="RoboTenon lightweight web portal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--metrics-wal", default="./runs/metrics.jsonl")
    parser.add_argument("--trace-wal", default="./runs/trace.jsonl")
    parser.add_argument("--trace-db", default=None)
    parser.add_argument(
        "--source", default=None, help="observability.json data source manifest"
    )
    parser.add_argument(
        "--run-root",
        default="./runs",
        help="root containing episode manifests and queue.sqlite3 files",
    )
    parser.add_argument(
        "--queue-db", default=None, help="optional explicit queue SQLite file"
    )
    parser.add_argument(
        "--observation-root",
        default=os.getenv("ROBOTENON_OBSERVATION_ROOT", "./data/observations"),
        help="observation cache file root",
    )
    parser.add_argument(
        "--observation-index",
        default=os.getenv("ROBOTENON_OBSERVATION_INDEX"),
        help="observation cache SQLite index",
    )
    parser.add_argument(
        "--grafana-url",
        default=os.getenv("ROBOTENON_GRAFANA_URL", DEFAULT_GRAFANA_URL),
        help="full Grafana dashboard URL to embed",
    )
    parser.add_argument(
        "--no-auto-grafana",
        action="store_true",
        help="do not start the local Grafana Compose services automatically",
    )
    parser.add_argument(
        "--retention-policy",
        help="opt in to hourly artifact cleanup using this policy JSON",
    )
    parser.add_argument("--max-connections", type=int, default=32)
    parser.add_argument("--max-queries", type=int, default=8)
    parser.add_argument("--control-slots", type=int, default=4)
    parser.add_argument(
        "--project-root",
        default=".",
        help="project root used for config discovery and experiment submission",
    )
    parser.add_argument(
        "--config-root",
        default="configs",
        help="directory containing selectable JSON configs (including subdirectories)",
    )
    parser.add_argument(
        "--experiment-output-root",
        default="data/experiments",
        help="root for submitted experiment logs and artifacts",
    )
    parser.add_argument(
        "--experiment-entrypoint",
        default="scripts/run_experiment.py",
        help="trusted experiment entrypoint",
    )
    parser.add_argument(
        "--experiment-python",
        default=None,
        help="Python executable used for submitted experiments",
    )
    parser.add_argument(
        "--no-experiment-submit",
        action="store_true",
        help="disable config discovery and experiment submission endpoints",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="additional trusted browser hostname (repeatable)",
    )
    parser.add_argument(
        "--loopback-published",
        action="store_true",
        help="container-only: host publishes this port exclusively on loopback",
    )
    args = parser.parse_args(argv)
    try:
        PortalAccess(
            args.host,
            control=not args.no_experiment_submit,
            token=(os.getenv("ROBOTENON_PORTAL_TOKEN") or os.getenv("ROBOSYS_PORTAL_TOKEN")),
            allowed_hosts=args.allowed_host,
            loopback_published=args.loopback_published,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.source:
        from ..runtime.observability import ObservabilitySource

        source = ObservabilitySource(
            **json.loads(Path(args.source).read_text(encoding="utf-8"))
        )
        if not source.trace_db:
            parser.error("source must contain trace_db")
        args.trace_db = source.trace_db
        args.trace_wal = source.trace_wal or str(
            Path(source.trace_db).with_suffix(".jsonl")
        )
        args.metrics_wal = source.metrics_wal
    auto_from_env = os.getenv("ROBOTENON_AUTO_GRAFANA", "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    grafana_url = (
        args.grafana_url or os.getenv("ROBOTENON_GRAFANA_URL") or DEFAULT_GRAFANA_URL
    )
    ensure_grafana(grafana_url, auto_start=auto_from_env and not args.no_auto_grafana)
    data = PortalData(
        args.metrics_wal,
        args.trace_wal,
        args.trace_db,
        run_root=args.run_root,
        queue_db=args.queue_db,
        observation_root=args.observation_root,
        observation_index=args.observation_index,
    )
    from ..runtime.retention import RetentionPolicy

    retention_policy = (
        RetentionPolicy(**json.loads(Path(args.retention_policy).read_text()))
        if args.retention_policy
        else None
    )
    experiment_manager = None
    if not args.no_experiment_submit:
        experiment_manager = ExperimentManager(
            project_root=args.project_root,
            config_root=args.config_root,
            output_root=args.experiment_output_root,
            entrypoint=args.experiment_entrypoint,
            python_executable=args.experiment_python,
            retention_policy=retention_policy,
        )
    try:
        server = build_server(
            args.host,
            args.port,
            data,
            grafana_url=grafana_url,
            experiment_manager=experiment_manager,
            allowed_hosts=tuple(args.allowed_host),
            control_token=(os.getenv("ROBOTENON_PORTAL_TOKEN") or os.getenv("ROBOSYS_PORTAL_TOKEN")),
            loopback_published=args.loopback_published,
            max_connections=args.max_connections,
            max_queries=args.max_queries,
            control_slots=args.control_slots,
        )
    except OSError as exc:
        data.traces.close()
        if exc.errno == errno.EADDRINUSE:
            raise SystemExit(
                "RoboTenon Portal cannot bind %s:%d: the address is already in use. "
                "Open the existing Portal, stop the process holding the port, or retry with "
                "`Tenon web --port 8766`." % (args.host, args.port)
            ) from exc
        raise
    print("RoboTenon Portal: http://%s:%d" % (args.host, server.server_port))
    print("Grafana embed: %s" % grafana_url)
    if experiment_manager is not None:
        experiment_manager.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if experiment_manager is not None:
            experiment_manager.close()
        server.server_close()
        data.traces.close()


if __name__ == "__main__":
    main()
