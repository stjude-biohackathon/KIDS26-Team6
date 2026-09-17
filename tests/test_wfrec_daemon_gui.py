"""Daemon GUI launch helpers."""

from __future__ import annotations

import pytest

from wfrec.daemon import _prefer_manual_browser_open, _wait_for_server


def test_prefer_manual_browser_when_lynx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROWSER", "lynx")
    monkeypatch.setenv("DISPLAY", ":0")
    assert _prefer_manual_browser_open() is True


def test_prefer_manual_browser_when_no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert _prefer_manual_browser_open() is True


def test_wait_for_server_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200

    def fake_get(url: str, timeout: float = 1.0) -> FakeResponse:
        calls["n"] += 1
        if calls["n"] >= 2:
            return FakeResponse()
        raise OSError("not yet")

    monkeypatch.setattr("httpx.get", fake_get)
    assert _wait_for_server("http://127.0.0.1:8787", timeout=2.0) is True
