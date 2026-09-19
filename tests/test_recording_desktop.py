"""Tests for dependency-free desktop file-manager integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocab.recording import desktop


def test_open_directory_uses_finder_on_macos(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "_launch_file_manager", commands.append)

    desktop.open_directory(tmp_path)

    assert commands == [["open", str(tmp_path.resolve())]]


def test_open_directory_uses_explorer_on_windows(tmp_path: Path, monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop.os, "startfile", opened.append, raising=False)

    desktop.open_directory(tmp_path)

    assert opened == [str(tmp_path.resolve())]


def test_open_directory_uses_xdg_open_on_linux(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(desktop.shutil, "which", lambda _command: "/usr/bin/xdg-open")
    monkeypatch.setattr(desktop, "_launch_file_manager", commands.append)

    desktop.open_directory(tmp_path)

    assert commands == [["/usr/bin/xdg-open", str(tmp_path.resolve())]]


def test_open_directory_rejects_a_headless_linux_host(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    with pytest.raises(desktop.DirectoryOpenError, match="No graphical desktop"):
        desktop.open_directory(tmp_path)


def test_open_directory_requires_an_existing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(desktop.DirectoryOpenError, match="does not exist"):
        desktop.open_directory(missing)
