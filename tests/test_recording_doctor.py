"""Metadata-only diagnostics for DevSQL-backed capture."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autocab.recording.collectors.shell import ShellBackendSelection
from autocab.recording.devsql import DevSQLClient
from autocab.recording.doctor import _agents_status, _shell_status, _source_details
from autocab.recording.state import SHELL_BACKEND_DEVSQL


def _metadata_client(queries: list[str]) -> DevSQLClient:
    """Return a DevSQL client that rejects any content-bearing query."""

    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        sql = command[-1]
        queries.append(sql)
        assert sql.endswith("LIMIT 0")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps([]),
            stderr="",
        )

    return DevSQLClient(Path("/test/devsql"), runner=runner)


def test_shell_status_reports_devsql_provider_and_fallback() -> None:
    queries: list[str] = []
    status = _shell_status(
        ShellBackendSelection(
            name=SHELL_BACKEND_DEVSQL,
            provider="atuin",
            client=_metadata_client(queries),
            devsql_version="devsql 0.5.1",
        )
    )
    status["fallback"] = "hook-spool available"

    assert status["available"] is True
    assert status["backend"] == "devsql"
    assert status["provider"] == "atuin"
    assert status["shell_output_available"] is False
    assert "provider: atuin" in _source_details(status)
    assert "fallback: hook-spool available" in _source_details(status)
    assert "output: unavailable" in _source_details(status)


def test_agent_diagnostics_only_probe_devsql_schema() -> None:
    queries: list[str] = []

    status = _agents_status(_metadata_client(queries))

    assert status["detected"]["devsql-codex"] is True
    assert "devsql-codex" in status["backend"]
    assert len(queries) == 1
    assert "JOIN codex_threads AS thread" in queries[0]
