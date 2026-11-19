"""Read models for Portal API queries."""

import json
import mimetypes
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from ..infra.metrics import MetricsDaemon
from ..infra.observation_cache import _as_uuid as _observation_session_uuid
from ..infra.run_index import RunManifestIndex
from ..infra.trace_query import TraceQuery

MAX_OBSERVATION_HISTORY_WINDOW = 100_000
MAX_OBSERVATION_HISTORY_RESULTS = 1_000
MAX_OBSERVATION_HISTORY_BYTES = 3 * 1024 * 1024
OBSERVATION_QUERY_TIMEOUT_S = 0.5
MAX_QUEUE_DATABASES_PER_QUERY = 256


class PortalData:
    def __init__(
        self,
        metrics_wal: str | Path,
        trace_wal: str | Path,
        trace_db: str | Path | None = None,
        run_root: str | Path = "./runs",
        queue_db: str | Path | None = None,
        observation_root: str | Path = "./data/observations",
        observation_index: str | Path | None = None,
    ):
        self.metrics = MetricsDaemon(metrics_wal, read_only=True)
        self.traces = TraceQuery(trace_db or Path(trace_wal).with_suffix(".sqlite3"))
        self.run_root = Path(run_root)
        self.run_index = RunManifestIndex(self.run_root)
        self.queue_db = Path(queue_db) if queue_db else None
        self.observation_root = Path(observation_root)
        self.observation_index = (
            Path(observation_index)
            if observation_index
            else self.observation_root / "index.sqlite3"
        )

    def runs(self, *, limit: int = 1000) -> list[dict]:
        if not self.run_root.exists():
            return []
        try:
            records = self.run_index.list(limit=limit)
            for record in records:
                indexed_path = Path(record["manifest_path"])
                try:
                    relative_path = indexed_path.relative_to(self.run_index.root)
                except ValueError:
                    continue
                record["manifest_path"] = str(self.run_root / relative_path)
            return records
        except (sqlite3.Error, OSError):
            # A damaged/unwritable catalog must not make existing run history
            # disappear. The scan is a compatibility fallback until the index
            # can be rebuilt.
            return self._scan_runs(limit)

    def _scan_runs(self, limit: int) -> list[dict]:
        records: list[dict] = []
        for path in self.run_root.glob("**/manifest.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                record["manifest_path"] = str(path)
                records.append(record)
            except Exception:
                continue
        records.sort(key=lambda item: item.get("started_at", ""))
        return records[-max(1, min(int(limit), 10_000)) :]

    def task_items(self, *, limit: int = 1000) -> list[dict]:
        limit = max(1, min(int(limit), 10_000))
        paths = self._queue_paths()
        records: list[dict] = []
        for path in paths:
            if len(records) >= limit:
                break
            if not path or not path.exists():
                continue
            connection = None
            try:
                connection = sqlite3.connect(str(path))
                connection.row_factory = sqlite3.Row
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_items'"
                ).fetchone()
                if table:
                    query = "SELECT * FROM task_items ORDER BY updated_at DESC LIMIT ?"
                else:
                    # Read-only migration view for an old local demo database;
                    # all new writes use TaskQueue's task_items table.
                    query = "SELECT subtask_id AS task_item_id, * FROM subtasks ORDER BY updated_at DESC LIMIT ?"
                rows = connection.execute(query, (limit - len(records),)).fetchall()
                for row in rows:
                    item = dict(row)
                    item["queue_db"] = str(path)
                    if "metadata_json" in item:
                        try:
                            item["metadata"] = json.loads(item.pop("metadata_json"))
                        except Exception:
                            pass
                    records.append(item)
            except (sqlite3.Error, OSError):
                continue
            finally:
                if connection is not None:
                    connection.close()
        return records[:limit]

    def task_item_count(self, *, limit: int = 10_000) -> int:
        """Return a bounded compatibility count without rediscovering the run tree."""

        limit = max(1, min(int(limit), 10_000))
        total = 0
        for path in self._queue_paths():
            if total >= limit:
                break
            if not path.is_file():
                continue
            connection = None
            try:
                connection = sqlite3.connect(str(path), timeout=0.1)
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('task_items', 'subtasks') ORDER BY name='task_items' DESC LIMIT 1"
                ).fetchone()
                if table:
                    total += int(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table[0]}"'
                        ).fetchone()[0]
                    )
            except (sqlite3.Error, OSError):
                continue
            finally:
                if connection is not None:
                    connection.close()
        return min(total, limit)

    def _queue_paths(self) -> list[Path]:
        if self.queue_db is not None:
            return [self.queue_db]
        try:
            return self.run_index.queue_paths(limit=MAX_QUEUE_DATABASES_PER_QUERY)
        except (sqlite3.Error, OSError):
            # Discovery fallback would recreate the unbounded recursive scan
            # this catalog exists to remove. An explicit --queue-db remains
            # available while a damaged catalog is rebuilt.
            return []

    def _observation_query(
        self,
        query: str,
        params: tuple = (),
        *,
        max_rows: int | None = None,
        max_bytes: int | None = None,
        timeout_s: float | None = None,
    ) -> list[dict]:
        """Read URI metadata from the cache index without opening media blobs."""

        if not self.observation_index.exists():
            return []
        connection = None
        try:
            timeout_s = timeout_s or OBSERVATION_QUERY_TIMEOUT_S
            deadline = time.monotonic() + timeout_s
            connection = sqlite3.connect(str(self.observation_index), timeout=timeout_s)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000
            )
            rows = []
            encoded_size = 0
            for row in connection.execute(query, params):
                if time.monotonic() >= deadline:
                    raise TimeoutError("observation query exceeded its time budget")
                value = dict(row)
                if max_rows is not None and len(rows) >= max_rows:
                    from ..infra.trace_query import QueryBudgetExceeded

                    raise QueryBudgetExceeded(
                        "observation query exceeded its row budget"
                    )
                if max_bytes is not None:
                    encoded_size += len(
                        json.dumps(value, ensure_ascii=False, default=str).encode(
                            "utf-8"
                        )
                    )
                    if encoded_size > max_bytes:
                        from ..infra.trace_query import QueryBudgetExceeded

                        raise QueryBudgetExceeded(
                            "observation query exceeded its decoded byte budget"
                        )
                rows.append(value)
            for row in rows:
                if row.get("feedback_json"):
                    try:
                        value = json.loads(row.pop("feedback_json"))
                        row.update(value)
                    except (TypeError, ValueError):
                        pass
                if row.get("shape_json"):
                    try:
                        row["shape"] = json.loads(row["shape_json"])
                    except (TypeError, ValueError):
                        pass
                row.pop("shape_json", None)
            return rows
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise TimeoutError(
                    "observation query exceeded its time budget"
                ) from exc
            return []
        except (sqlite3.Error, OSError):
            return []
        finally:
            if connection is not None:
                connection.close()

    def observation_latest(
        self, session_id: str, *, epoch: int | None = None
    ) -> dict | None:
        sid = str(_observation_session_uuid(session_id))
        query = (
            "SELECT feedback_json FROM observation_records WHERE session_id=? "
            + ("AND epoch=? " if epoch is not None else "")
            + "ORDER BY epoch DESC, sequence DESC LIMIT 1"
        )
        rows = self._observation_query(
            query, (sid, int(epoch)) if epoch is not None else (sid,)
        )
        return rows[0] if rows else None

    def observation_history(
        self,
        session_id: str,
        *,
        epoch: int | None = None,
        length: int = 4,
        interval: int = 1,
    ) -> list[dict]:
        if length <= 0 or interval <= 0:
            raise ValueError("length and interval must be positive")
        if length > MAX_OBSERVATION_HISTORY_RESULTS:
            raise ValueError(
                "length exceeds the observation history result limit "
                f"of {MAX_OBSERVATION_HISTORY_RESULTS}"
            )
        count = length * interval
        if count > MAX_OBSERVATION_HISTORY_WINDOW:
            raise ValueError(
                "length * interval exceeds the observation history limit "
                f"of {MAX_OBSERVATION_HISTORY_WINDOW} records"
            )
        sid = str(_observation_session_uuid(session_id))
        query = (
            "WITH recent AS ("
            " SELECT feedback_json, ROW_NUMBER() OVER ("
            " ORDER BY epoch DESC, sequence DESC) AS ordinal"
            " FROM (SELECT feedback_json, epoch, sequence FROM observation_records "
            "WHERE session_id=? "
            + ("AND epoch=? " if epoch is not None else "")
            + "ORDER BY epoch DESC, sequence DESC LIMIT ?)"
            ") SELECT feedback_json FROM recent "
            "WHERE (ordinal - 1) % ? = 0 ORDER BY ordinal DESC"
        )
        params = (
            (sid, int(epoch), count, interval)
            if epoch is not None
            else (sid, count, interval)
        )
        return self._observation_query(
            query,
            params,
            max_rows=length,
            max_bytes=MAX_OBSERVATION_HISTORY_BYTES,
            timeout_s=OBSERVATION_QUERY_TIMEOUT_S,
        )

    def observation_metadata(self, uri: str) -> dict | None:
        rows = self._observation_query(
            """
            SELECT modality, uri, media_type, encoding, shape_json, dtype,
                   sha256, size_bytes, storage_path
            FROM observation_frames WHERE uri=?
            """,
            (str(uri),),
        )
        return rows[0] if rows else None

    def observation_image(
        self,
        uri: str,
        *,
        output_format: str = "",
    ) -> tuple[bytes, str] | None:
        """Materialise one cached RGB observation as browser-displayable PNG."""

        metadata = self.observation_metadata(uri)
        if not metadata or not metadata.get("storage_path"):
            return None
        root = self.observation_root.resolve()
        try:
            path = Path(str(metadata["storage_path"])).resolve()
            path.relative_to(root)
        except (OSError, ValueError):
            return None
        return self.image_from_path(
            path,
            media_type=str(metadata.get("media_type") or ""),
            encoding=str(metadata.get("encoding") or ""),
            output_format=output_format,
        )

    @staticmethod
    @lru_cache(maxsize=128)
    def _image_from_path_cached(
        path_text: str,
        mtime_ns: int,
        size: int,
        media_type: str,
        encoding: str,
        output_format: str,
    ) -> tuple[bytes, str] | None:
        """Read/convert one immutable preview and retain it for live polling.

        Observation frames are content-addressed in normal operation.  The
        mtime/size arguments also make the cache safe when a writer replaces a
        file in place during a running episode.
        """

        path = Path(path_text)
        if not path.is_file():
            return None
        media_type = media_type or mimetypes.guess_type(path.name)[0] or ""
        if media_type.startswith("image/") and path.suffix.lower() != ".npy":
            try:
                return path.read_bytes(), media_type
            except OSError:
                return None
        if str(encoding or "").lower() != "npy" and path.suffix.lower() != ".npy":
            return None
        try:
            import io

            import numpy as np
            from PIL import Image

            array = np.load(path, allow_pickle=False)
            if array.ndim == 3 and array.shape[-1] == 1:
                array = array[..., 0]
            if array.ndim not in {2, 3}:
                return None
            if array.ndim == 3 and array.shape[-1] not in {3, 4}:
                return None
            if array.dtype != np.uint8:
                values = np.asarray(array, dtype=np.float32)
                finite = np.isfinite(values)
                if not finite.any():
                    values = np.zeros_like(values)
                else:
                    low = float(values[finite].min())
                    high = float(values[finite].max())
                    if low >= 0.0 and high <= 1.0:
                        values = values * 255.0
                    elif high > low:
                        values = (values - low) * (255.0 / (high - low))
                    else:
                        values = np.zeros_like(values)
                array = np.clip(values, 0, 255).astype(np.uint8)
            requested = str(output_format or "").lower()
            use_jpeg = (
                requested in {"jpg", "jpeg"}
                and array.ndim == 3
                and array.shape[-1] in {3, 4}
            )
            if use_jpeg and array.shape[-1] == 4:
                array = array[..., :3]
            output = io.BytesIO()
            if use_jpeg:
                # Preview images travel through an SSH tunnel; JPEG is much
                # smaller than PNG while retaining enough detail for the UI.
                Image.fromarray(array).save(
                    output, format="JPEG", quality=82, optimize=False
                )
                return output.getvalue(), "image/jpeg"
            Image.fromarray(array).save(output, format="PNG", optimize=False)
            return output.getvalue(), "image/png"
        except (ImportError, OSError, ValueError):
            return None

    @classmethod
    def image_from_path(
        cls,
        path: str | Path,
        *,
        media_type: str = "",
        encoding: str = "",
        output_format: str = "",
    ) -> tuple[bytes, str] | None:
        """Materialise a trusted image artifact, including numpy observations."""

        path = Path(path).resolve()
        if not path.is_file():
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return cls._image_from_path_cached(
            str(path),
            int(stat.st_mtime_ns),
            int(stat.st_size),
            str(media_type or ""),
            str(encoding or ""),
            str(output_format or ""),
        )
