"""HTTP client for the control API, with a documented direct-mode fallback.

The CLI prefers the daemon so that a command and a click do the same thing. But
a recorder that is unusable when its daemon is not running is a bad tool, so
lifecycle operations fall back to acting on the session store directly. The
fallback is explicit and reported, never silent, because in direct mode nothing
is capturing in the background.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .daemon import daemon_alive
from .state import read_api


class DaemonUnavailable(RuntimeError):
    """Raised when an operation genuinely requires the daemon."""


class Client:
    """Minimal typed wrapper over the control API."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token

    @classmethod
    def discover(cls, *, require: bool = False) -> "Client | None":
        """Return a client if a daemon is responding, else ``None``."""

        api = read_api()
        if not api:
            if require:
                raise DaemonUnavailable(
                    "No AutoCAB recording service is running. "
                    "Start one with `autocab dashboard`."
                )
            return None
        if daemon_alive() is None:
            if require:
                raise DaemonUnavailable(
                    "The AutoCAB recording service is published but not responding. "
                    "Restart it with `autocab dashboard`."
                )
            return None
        return cls(api[0], api[1])

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        import httpx

        response = httpx.request(
            method,
            f"{self.url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30.0,
        )
        if response.status_code >= 400:
            detail = response.text
            with contextlib.suppress(Exception):
                detail = response.json().get("detail", detail)
            raise RuntimeError(f"AutoCAB API {response.status_code}: {detail}")
        return response.json()

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict | None = None) -> Any:
        return self._request("POST", path, payload or {})
