"""Tests for bounded local dependency-version discovery."""

from __future__ import annotations

from importlib import metadata
from subprocess import CompletedProcess, TimeoutExpired

from autocab.workflow import dependency_versions
from autocab.workflow.builder import _executable


def test_shell_syntax_is_not_classified_as_a_dependency():
    """Comments, assignments, and common shell commands should not enter review."""

    assert _executable("# review this later") is None
    assert _executable("RESULT=ready echo $RESULT") is None
    assert _executable("mkdir output") is None
    assert _executable("RESULT=ready git status") == "git"


def test_python_console_script_metadata_is_preferred(monkeypatch):
    """Installed metadata should avoid starting the command when available."""

    class Distribution:
        version = "1.4.2"
        metadata = {"Name": "example-package"}

    class EntryPoint:
        name = "example"
        dist = Distribution()

    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [EntryPoint()])

    detected = dependency_versions.detect_dependency_version("example")

    assert detected is not None
    assert detected.value == "1.4.2"
    assert detected.source == "installed Python distribution example-package"


def test_running_python_version_is_available_without_a_probe():
    """Python commands should use the interpreter already running AutoCAB."""

    detected = dependency_versions.detect_dependency_version("python")

    assert detected is not None
    assert detected.value.count(".") == 2
    assert detected.source == "running Python interpreter"


def test_known_tool_version_probe_has_no_shell(monkeypatch):
    """Known tools should use an argument list and a short timeout."""

    def missing_distribution(name):
        raise metadata.PackageNotFoundError(name)

    captured = {}

    def run(command, **options):
        captured["command"] = command
        captured["options"] = options
        return CompletedProcess(command, 0, stdout="git version 2.51.0\n", stderr="")

    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(metadata, "version", missing_distribution)
    monkeypatch.setattr(dependency_versions.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(dependency_versions.subprocess, "run", run)

    detected = dependency_versions.detect_dependency_version("git")

    assert detected is not None
    assert detected.value == "2.51.0"
    assert captured["command"] == ["/bin/git", "--version"]
    assert captured["options"]["timeout"] == 3
    assert "shell" not in captured["options"]


def test_unknown_or_slow_tools_remain_unresolved(monkeypatch):
    """Discovery should neither run unknown tools nor wait on a stalled probe."""

    def missing_distribution(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(metadata, "version", missing_distribution)
    monkeypatch.setattr(dependency_versions.shutil, "which", lambda name: f"/bin/{name}")
    assert dependency_versions.detect_dependency_version("unknown-tool") is None

    monkeypatch.setattr(
        dependency_versions.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutExpired(cmd=args[0], timeout=3)),
    )
    assert dependency_versions.detect_dependency_version("git") is None
