"""A tiny JSON-over-HTTP helper on the standard library (urllib), used by
the worker and the client. No third-party HTTP library is needed on the laptop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class NetworkError(RuntimeError):
    """The server could not be reached (down, wrong URL, timeout...)."""


@dataclass(frozen=True)
class Reply:
    status: int
    data: Any  # parsed JSON, or None for an empty body (e.g. 204)


def _opener(url: str) -> urllib.request.OpenerDirector:
    # A local server must never be reached through an HTTP proxy from the
    # environment; remote servers use the normal proxy settings.
    if urlsplit(url).hostname in LOOPBACK_HOSTS:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def request_json(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
                 timeout: float = 10.0) -> Reply:
    """Send one request. HTTP error statuses are RETURNED (not raised);
    only network problems raise NetworkError."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _opener(url).open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:  # 4xx / 5xx: still a reply
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise NetworkError(f"cannot reach {url}: {exc}") from exc
    if not raw:
        return Reply(status, None)
    try:
        return Reply(status, json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Reply(status, {"detail": raw[:200].decode("utf-8", "replace")})
