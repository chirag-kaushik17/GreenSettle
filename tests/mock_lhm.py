"""A tiny fake LibreHardwareMonitor web server for tests (works on any OS).

It serves a JSON tree at /data.json. Tests can change the CPU Package watts
while the server runs, to simulate load.

Can also be run by hand:  python tests/mock_lhm.py --port 8085 --watts 4.5
"""

from __future__ import annotations

import argparse
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "lhm_data_sample.json"


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def set_package_watts(tree: dict[str, Any], watts: float | None) -> None:
    """Change the CPU Package sensor in place (RawValue and formatted Value)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("SensorId", "").endswith("/power/0") and node.get("Text") == "CPU Package":
            node["RawValue"] = watts
            node["Value"] = "-" if watts is None else f"{watts:.1f} W"
            return
        stack.extend(node.get("Children") or [])
    raise KeyError("CPU Package sensor not found in tree")


class MockLhmServer:
    """Start with .start(), read .url, change .package_watts, stop with .stop()."""

    def __init__(self, tree: dict[str, Any] | None = None, port: int = 0) -> None:
        self.tree = copy.deepcopy(tree) if tree is not None else load_fixture()
        self.requests = 0
        self._lock = threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server naming)
                if self.path.split("?")[0] != "/data.json":
                    self.send_error(404)
                    return
                with server._lock:
                    server.requests += 1
                    body = json.dumps(server.tree).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/data.json"

    def set_package_watts(self, watts: float | None) -> None:
        with self._lock:
            set_package_watts(self.tree, watts)

    def start(self) -> "MockLhmServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8085)
    parser.add_argument("--watts", type=float, default=4.5)
    a = parser.parse_args()
    srv = MockLhmServer(port=a.port)
    srv.set_package_watts(a.watts)
    print(f"Mock LHM serving {srv.url} (Ctrl+C to stop)")
    srv.start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
