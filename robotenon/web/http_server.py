"""Bounded HTTP connections and separate query/control admission budgets."""

import json
import threading
from http.server import ThreadingHTTPServer


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address, handler, *, max_connections=32, max_queries=8, control_slots=4
    ):
        if (
            min(max_queries, control_slots) < 1
            or max_connections < max_queries + control_slots
        ):
            raise ValueError(
                "connections must cover positive query and reserved control limits"
            )
        self._budget_lock = threading.Lock()
        self._limits = {
            "connections": max_connections,
            "queries": max_queries,
            "controls": control_slots,
        }
        self._active = {key: 0 for key in self._limits}
        self._rejected = 0
        super().__init__(address, handler)

    def acquire(self, kind):
        with self._budget_lock:
            if self._active[kind] >= self._limits[kind]:
                self._rejected += 1
                return False
            self._active[kind] += 1
            return True

    def release(self, kind):
        with self._budget_lock:
            self._active[kind] -= 1

    def admission_snapshot(self):
        with self._budget_lock:
            return {
                "limits": dict(self._limits),
                "active": dict(self._active),
                "rejected": self._rejected,
            }

    def process_request(self, request, client_address):
        if not self.acquire("connections"):
            body = json.dumps({"error": "Portal is busy; retry shortly"}).encode()
            try:
                request.settimeout(0.05)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Retry-After: 1\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.release("connections")
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release("connections")
