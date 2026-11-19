"""Request boundary for the Portal's local and explicitly exposed endpoints."""

from __future__ import annotations

from urllib.parse import urlsplit
import base64
import binascii
import hmac


class PortalAccess:
    def __init__(self, host: str, *, control: bool, token=None, allowed_hosts=(), loopback_published=False):
        if host not in {"localhost", "127.0.0.1", "::1"} and not token and not loopback_published:
            raise ValueError("non-loopback Portal requires ROBOTENON_PORTAL_TOKEN; use SSH for local access")
        self.token = token or None
        self.allowed_hosts = {"localhost", "127.0.0.1", "::1"}
        if host not in {"", "0.0.0.0", "::"}:
            self.allowed_hosts.add(host.lower())
        for value in allowed_hosts:
            parsed = self._authority(value)
            self.allowed_hosts.add(parsed.hostname.lower())

    @staticmethod
    def _authority(value):
        parsed = urlsplit("//" + value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid Host")
        # Validate the optional port, including malformed/non-numeric values.
        _ = parsed.port
        return parsed

    def check(self, headers, *, write=False, control=False):
        try:
            host = self._authority(headers.get("Host", ""))
            if host.hostname.lower() not in self.allowed_hosts:
                return 403, "untrusted Host"
            origin_value = headers.get("Origin")
            if origin_value is not None:
                origin = urlsplit(origin_value)
                if (
                    origin.scheme not in {"http", "https"}
                    or origin.path
                    or origin.query
                    or origin.fragment
                    or origin.netloc.lower() != host.netloc.lower()
                ):
                    return 403, "cross-origin requests are not allowed"
        except ValueError:
            return 403, "invalid request origin or Host"
        if self.token:
            authorization = headers.get("Authorization", "")
            supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
            if authorization.startswith("Basic "):
                try:
                    supplied = base64.b64decode(authorization[6:], validate=True).decode().split(":", 1)[1]
                except (ValueError, IndexError, UnicodeError, binascii.Error):
                    supplied = ""
            if not hmac.compare_digest(supplied.encode(), self.token.encode()):
                return 401, "authentication required"
        if write and headers.get_content_type() != "application/json":
            return 415, "Content-Type must be application/json"
        return None
