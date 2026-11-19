"""Durable storage primitives for the system-level observation cache.

The store knows how to materialise common sensor payloads, but it does not
know anything about an EnvironmentAdapter.  Payloads are written with
``tempfile -> fsync -> os.replace`` so a process crash cannot expose a
partially written image/array to a resolver.  SQLite metadata and cache URI
mapping live in :mod:`observation_cache`, keeping this class useful for a
shared filesystem or an object-store implementation later.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_MEDIA_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "application/x-npy": ".npy",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/octet-stream": ".bin",
}


@dataclass(frozen=True, slots=True)
class PayloadDescription:
    """Metadata discovered without copying the payload."""

    media_type: str | None = None
    encoding: str | None = None
    shape: list[int] | None = None
    dtype: str | None = None
    extension: str = ".bin"
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Result of an atomic file write."""

    storage_path: Path
    media_type: str | None
    encoding: str | None
    shape: list[int] | None
    dtype: str | None
    sha256: str
    size_bytes: int


def safe_component(value: Any, *, fallback: str = "item") -> str:
    """Return a path component that cannot escape the store root."""

    text = _SAFE_COMPONENT.sub("_", str(value)).strip("._")
    return text or fallback


def _extension_for(media_type: str | None, source: str | None = None) -> str:
    if media_type:
        value = _MEDIA_EXTENSIONS.get(media_type.lower())
        if value:
            return value
        guessed = mimetypes.guess_extension(media_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    if source:
        suffix = Path(urllib.parse.urlparse(source).path).suffix
        if suffix and re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
            return suffix.lower()
    return ".bin"


def _guess_media_type(source: str | None, default: str | None = None) -> str | None:
    if default:
        return default
    if source:
        guessed, _ = mimetypes.guess_type(urllib.parse.urlparse(source).path)
        if guessed:
            return guessed
    return None


def _is_numpy_array(value: Any) -> bool:
    module = type(value).__module__
    return module == "numpy" or module.startswith("numpy.") or (
        hasattr(value, "shape") and hasattr(value, "dtype") and hasattr(value, "tobytes")
    )


def _payload_jsonable(value: Any) -> Any:
    """Convert nested sensor state to lossless, JSON-decodable primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "__bytes_base64__": base64.b64encode(bytes(value)).decode("ascii")
        }
    if _is_numpy_array(value):
        tolist = getattr(value, "tolist", None)
        if callable(tolist):
            return _payload_jsonable(tolist())
        return list(bytes(value.tobytes()))
    if isinstance(value, Mapping):
        return {str(key): _payload_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_payload_jsonable(item) for item in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _payload_jsonable(tolist())
        except Exception:
            pass
    return str(value)


def describe_payload(
    payload: Any,
    *,
    modality: str | None = None,
    media_type: str | None = None,
    encoding: str | None = None,
) -> PayloadDescription:
    """Inspect a payload and return URI metadata without serialising it."""

    if payload is None:
        return PayloadDescription(media_type=media_type, encoding=encoding, extension=".json", size_bytes=4)
    if isinstance(payload, (bytes, bytearray, memoryview)):
        media = media_type or ("image/jpeg" if modality == "rgb" else None)
        raw = bytes(payload)
        return PayloadDescription(
            media_type=media,
            encoding=encoding,
            extension=_extension_for(media),
            size_bytes=len(raw),
        )
    if _is_numpy_array(payload):
        shape = [int(item) for item in getattr(payload, "shape", ())]
        dtype = str(getattr(payload, "dtype", "")) or None
        nbytes = getattr(payload, "nbytes", None)
        return PayloadDescription(
            media_type=media_type or "application/x-npy",
            encoding=encoding or "npy",
            shape=shape or None,
            dtype=dtype,
            extension=".npy",
            size_bytes=int(nbytes) if nbytes is not None else None,
        )
    if isinstance(payload, (str, os.PathLike)):
        text = os.fspath(payload)
        if text.startswith(("http://", "https://")):
            media = _guess_media_type(text, media_type)
            return PayloadDescription(
                media_type=media,
                encoding=encoding,
                extension=_extension_for(media, text),
            )
        path = Path(text)
        if path.exists() and path.is_file():
            media = _guess_media_type(str(path), media_type)
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            return PayloadDescription(
                media_type=media,
                encoding=encoding,
                extension=path.suffix.lower() or _extension_for(media),
                size_bytes=size,
            )
        if text.startswith("file://"):
            parsed = urllib.parse.urlparse(text)
            local_path = Path(urllib.parse.unquote(parsed.path))
            media = _guess_media_type(str(local_path), media_type)
            try:
                size = local_path.stat().st_size
            except OSError:
                size = None
            return PayloadDescription(
                media_type=media,
                encoding=encoding,
                extension=local_path.suffix.lower() or _extension_for(media),
                size_bytes=size,
            )
        raw = text.encode("utf-8")
        return PayloadDescription(
            media_type=media_type or ("text/plain" if modality in {"text", "proprioception"} else "text/plain"),
            encoding=encoding or "utf-8",
            extension=_extension_for(media_type or "text/plain"),
            size_bytes=len(raw),
        )
    if isinstance(payload, Mapping) or isinstance(payload, (list, tuple, set, int, float, bool)):
        try:
            raw = json.dumps(_payload_jsonable(payload), ensure_ascii=False, default=str).encode("utf-8")
            size = len(raw)
        except (TypeError, ValueError):
            size = None
        return PayloadDescription(
            media_type=media_type or "application/json",
            encoding=encoding or "utf-8",
            extension=_extension_for(media_type or "application/json"),
            size_bytes=size,
        )
    raw_size = getattr(payload, "nbytes", None)
    return PayloadDescription(
        media_type=media_type or "application/octet-stream",
        encoding=encoding,
        extension=_extension_for(media_type or "application/octet-stream"),
        size_bytes=int(raw_size) if raw_size is not None else None,
    )


def payload_to_bytes(payload: Any, *, timeout: float = 10.0) -> bytes:
    """Materialise one supported payload as bytes in the persistence worker."""

    if payload is None:
        return b"null"
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, (bytearray, memoryview)):
        return bytes(payload)
    if _is_numpy_array(payload):
        try:
            import numpy as np  # type: ignore[import-not-found]

            stream = io.BytesIO()
            np.save(stream, payload, allow_pickle=False)
            return stream.getvalue()
        except Exception:
            # A numpy-like object can still provide a stable byte view even
            # when numpy is not installed in the worker environment.
            return bytes(payload.tobytes())
    if isinstance(payload, (str, os.PathLike)):
        text = os.fspath(payload)
        if text.startswith(("http://", "https://")):
            with urllib.request.urlopen(text, timeout=timeout) as response:
                return response.read()
        if text.startswith("file://"):
            parsed = urllib.parse.urlparse(text)
            return Path(urllib.parse.unquote(parsed.path)).read_bytes()
        path = Path(text)
        if path.exists() and path.is_file():
            return path.read_bytes()
        return text.encode("utf-8")
    if isinstance(payload, Mapping) or isinstance(payload, (list, tuple, set, int, float, bool)):
        return json.dumps(
            _payload_jsonable(payload),
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    for method_name in ("to_bytes", "tobytes"):
        method = getattr(payload, method_name, None)
        if callable(method):
            value = method()
            return value if isinstance(value, bytes) else bytes(value)
    return repr(payload).encode("utf-8")


class FileStore:
    """Atomic filesystem backend used by :class:`ObservationCache`."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(
        self,
        *,
        session_id: Any,
        epoch: int,
        sequence: int,
        frame_name: str,
        extension: str = ".bin",
    ) -> Path:
        ext = extension if extension.startswith(".") else f".{extension}"
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
            raise ValueError(f"invalid observation file extension: {extension!r}")
        directory = (
            self.root
            / safe_component(session_id, fallback="session")
            / f"epoch-{int(epoch)}"
            / f"{int(sequence)}"
        )
        return directory / f"{safe_component(frame_name)}{ext.lower()}"

    def put(
        self,
        payload: Any,
        path: str | os.PathLike[str],
        *,
        media_type: str | None = None,
        encoding: str | None = None,
        shape: list[int] | None = None,
        dtype: str | None = None,
    ) -> StoredObject:
        """Write *payload* atomically and return checksum/size metadata."""

        import hashlib

        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw = payload_to_bytes(payload)
        digest = hashlib.sha256(raw).hexdigest()
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            # fsync the directory where supported; this closes the small
            # rename durability window on POSIX filesystems.
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return StoredObject(
            storage_path=destination,
            media_type=media_type,
            encoding=encoding,
            shape=shape,
            dtype=dtype,
            sha256=digest,
            size_bytes=len(raw),
        )

    @staticmethod
    def read(path: str | os.PathLike[str]) -> bytes:
        return Path(path).read_bytes()

    # Explicit spellings are convenient for custom persistence workers and
    # preserve the small backend surface if FileStore is replaced by an
    # object-store implementation.
    put_bytes = put
    get = read

    @staticmethod
    def delete(path: str | os.PathLike[str]) -> None:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "FileStore",
    "PayloadDescription",
    "StoredObject",
    "describe_payload",
    "payload_to_bytes",
    "safe_component",
]
