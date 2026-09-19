"""Selection and persistence of the local shell capture backend."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from wfrec import spool
from wfrec.collectors.shell import (
    DEVSQL_SHELL_SOURCE,
    ShellBackendSelection,
    ShellCollector,
    resolve_shell_backend,
)
from wfrec.devsql import DevSQLClient, DevSQLUnavailableError
from wfrec.events import SHELL_COMMAND
from wfrec.recorder import Recorder
from wfrec.session import Session, SessionStore
from wfrec.state import (
    SHELL_BACKEND_DEVSQL,
    SHELL_BACKEND_SPOOL,
    read_sentinel,
    sentinel_enables,
)


DISABLED_BACKGROUND_SOURCES = {
    "screen": False,
    "context": False,
    "agents": False,
    "files": False,
}


def test_resolver_selects_devsql_after_content_free_probe() -> None:
    commands: list[list[str]] = []
    client = _devsql_client(commands=commands)

    selection = resolve_shell_backend(discover=lambda: client)

    assert selection.name == SHELL_BACKEND_DEVSQL
    assert selection.provider == DEVSQL_SHELL_SOURCE
    assert selection.devsql_version == "devsql 0.5.1"
    assert commands[0][-1] == "--version"
    assert commands[1][-1].endswith("FROM command_events LIMIT 0")


def test_resolver_falls_back_when_devsql_is_unavailable() -> None:
    def missing_devsql() -> DevSQLClient:
        raise DevSQLUnavailableError("DevSQL executable is not available.")

    selection = resolve_shell_backend(discover=missing_devsql)

    assert selection.name == SHELL_BACKEND_SPOOL
    assert "not available" in selection.detail


def test_restore_does_not_switch_a_devsql_interval_to_spool() -> None:
    def missing_devsql() -> DevSQLClient:
        raise DevSQLUnavailableError("DevSQL executable is not available.")

    selection = resolve_shell_backend(
        SHELL_BACKEND_DEVSQL,
        discover=missing_devsql,
    )

    assert selection.name == SHELL_BACKEND_DEVSQL
    assert selection.client is None


def test_recorder_selects_devsql_and_suppresses_local_hook(
    wfrec_home: Path,
) -> None:
    selection = _devsql_selection()
    recorder = Recorder(
        shell_backend_resolver=lambda _backend: selection,
    )
    try:
        status = recorder.start_session(
            title="DevSQL",
            sources=DISABLED_BACKGROUND_SOURCES,
        )

        session = Session.load(status["session"]["id"])
        flags = read_sentinel()[1]
        assert session.manifest.shell_backend == SHELL_BACKEND_DEVSQL
        assert status["shell_backend"] == SHELL_BACKEND_DEVSQL
        assert status["collectors"]["shell"]["backend"] == "devsql"
        assert status["shell_output_available"] is False
        assert "DevSQL" in status["shell_output_reason"]
        assert not sentinel_enables(flags, "shell")
        with pytest.raises(ValueError, match="DevSQL"):
            recorder.set_shell_output(True)
    finally:
        recorder.stop_session()


def test_missing_devsql_keeps_hook_spool_active(wfrec_home: Path) -> None:
    recorder = Recorder(shell_backend_resolver=_unavailable_selection)
    try:
        status = recorder.start_session(
            title="Fallback",
            sources=DISABLED_BACKGROUND_SOURCES,
        )

        flags = read_sentinel()[1]
        assert status["shell_backend"] == SHELL_BACKEND_SPOOL
        assert status["collectors"]["shell"]["backend"] == "hook-spool"
        assert sentinel_enables(flags, "shell")
    finally:
        recorder.stop_session()


def test_no_daemon_forces_hook_spool(wfrec_home: Path) -> None:
    recorder = Recorder(
        supervise=False,
        shell_backend_resolver=lambda _backend: _devsql_selection(),
    )

    status = recorder.start_session(title="No daemon")

    assert status["shell_backend"] == SHELL_BACKEND_SPOOL
    assert sentinel_enables(read_sentinel()[1], "shell")
    recorder.stop_session()


def test_resume_reselects_backend(wfrec_home: Path) -> None:
    current = ShellBackendSelection(name=SHELL_BACKEND_SPOOL)

    def resolve(_backend: str | None) -> ShellBackendSelection:
        return current

    recorder = Recorder(shell_backend_resolver=resolve)
    try:
        started = recorder.start_session(
            title="Resume",
            sources=DISABLED_BACKGROUND_SOURCES,
        )
        session_id = started["session"]["id"]
        recorder.pause_session()

        current = _devsql_selection()
        resumed = recorder.resume_session(session_id)

        session = Session.load(session_id)
        assert resumed["shell_backend"] == SHELL_BACKEND_DEVSQL
        assert session.manifest.shell_backend == SHELL_BACKEND_DEVSQL
        assert not sentinel_enables(read_sentinel()[1], "shell")
    finally:
        recorder.stop_session()


def test_restart_reports_persisted_devsql_backend_as_degraded(
    store: SessionStore,
) -> None:
    session, _ = store.start(
        title="Restart",
        shell_backend=SHELL_BACKEND_DEVSQL,
    )
    recorder = Recorder(shell_backend_resolver=_unavailable_selection)
    recorder.attach(session)
    recorder._start_collector("shell")
    try:
        status = recorder.status()

        assert status["shell_backend"] == SHELL_BACKEND_DEVSQL
        assert status["collectors"]["shell"]["backend"] == "devsql"
        assert status["collectors"]["shell"]["available"] is False
        assert not sentinel_enables(read_sentinel()[1], "shell")
    finally:
        recorder.stop_session()


def test_devsql_collector_reads_remote_without_draining_local_spool(
    store: SessionStore,
    tmp_path: Path,
) -> None:
    session, _ = store.start(
        title="No duplicate spool",
        shell_backend=SHELL_BACKEND_DEVSQL,
    )
    spool.append(
        spool.spool_file(session.spool_dir, "host", 10),
        spool.encode(
            spool.KIND_COMMAND,
            "1",
            "/tmp",
            "0",
            "1",
            "should-not-be-read",
            "bash",
            "10",
            "1",
        ),
    )
    remote_dir = tmp_path / "remote"
    spool.append(
        spool.spool_file(remote_dir, "login2", 20),
        spool.encode(
            spool.KIND_COMMAND,
            "2",
            "/scratch",
            "0",
            "2",
            "remote-command",
            "sh",
            "20",
            "1",
        ),
    )
    selection = _devsql_selection()
    collector = ShellCollector(
        session,
        extra_spools={remote_dir: "remote:hpc"},
        devsql_client=selection.client,
        local_backend=SHELL_BACKEND_DEVSQL,
        devsql_version=selection.devsql_version,
    )

    collector._run_once()

    shell_events = [event for event in session.writer.read() if event.type == SHELL_COMMAND]
    assert len(shell_events) == 1
    assert shell_events[0].origin == "remote:hpc"
    assert shell_events[0].payload["command"] == "remote-command"


def _devsql_selection() -> ShellBackendSelection:
    client = _devsql_client()
    return ShellBackendSelection(
        name=SHELL_BACKEND_DEVSQL,
        provider=DEVSQL_SHELL_SOURCE,
        client=client,
        devsql_version="devsql 0.5.1",
    )


def _unavailable_selection(
    backend: str | None,
) -> ShellBackendSelection:
    return ShellBackendSelection(
        name=backend or SHELL_BACKEND_SPOOL,
        provider=(DEVSQL_SHELL_SOURCE if backend == SHELL_BACKEND_DEVSQL else ""),
        detail="DevSQL executable is not available.",
    )


def _devsql_client(
    *,
    commands: list[list[str]] | None = None,
) -> DevSQLClient:
    """Return a client that exposes metadata but no activity rows."""

    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        if commands is not None:
            commands.append(command)
        stdout = "devsql 0.5.1\n" if command[-1] == "--version" else json.dumps([])
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    return DevSQLClient(Path("/test/devsql"), runner=runner)
