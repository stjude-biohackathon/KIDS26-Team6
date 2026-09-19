"""Safe DevSQL process and response handling."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wfrec.devsql import (
    DevSQLClient,
    DevSQLExecutionError,
    DevSQLQueryError,
    DevSQLResponseError,
    DevSQLTimeoutError,
    DevSQLUnavailableError,
)


def completed_process(
    *,
    stdout: str = "",
    stderr: str = "",
    return_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Return a predictable DevSQL process result for one unit test."""

    return subprocess.CompletedProcess(
        args=["devsql"],
        returncode=return_code,
        stdout=stdout,
        stderr=stderr,
    )


def test_discover_finds_devsql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wfrec.devsql.shutil.which", lambda name: "/opt/bin/devsql")

    client = DevSQLClient.discover()

    assert client.executable == Path("/opt/bin/devsql")


def test_discover_reports_missing_devsql(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wfrec.devsql.shutil.which", lambda name: None)

    with pytest.raises(
        DevSQLUnavailableError,
        match="DevSQL executable is not available",
    ):
        DevSQLClient.discover()


def test_version_uses_bounded_subprocess_options() -> None:
    captured: dict[str, object] = {}

    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["options"] = options
        return completed_process(stdout="devsql 1.2.3\n")

    client = DevSQLClient(
        executable=Path("/test/devsql"),
        timeout_seconds=12,
        runner=runner,
    )

    assert client.version() == "devsql 1.2.3"
    assert captured["command"] == ["/test/devsql", "--version"]
    assert captured["options"] == {
        "check": True,
        "capture_output": True,
        "encoding": "utf-8",
        "timeout": 12,
        "errors": "replace",
        "shell": False,
        "stdin": subprocess.DEVNULL,
    }


def test_query_returns_validated_rows() -> None:
    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        return completed_process(stdout='[{"source":"atuin","timestamp":123}]')

    client = DevSQLClient(Path("devsql"), runner=runner)

    rows = client.query(
        "SELECT source, timestamp FROM command_events",
        required_columns={"source", "timestamp"},
    )

    assert rows == [{"source": "atuin", "timestamp": 123}]


def test_client_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        DevSQLClient(Path("devsql"), timeout_seconds=0)


@pytest.mark.parametrize("query", ["", "DELETE FROM command_events"])
def test_query_rejects_empty_or_non_select_sql(query: str) -> None:
    client = DevSQLClient(Path("devsql"))

    with pytest.raises(DevSQLQueryError):
        client.query(query)


def test_query_reports_timeout_without_process_output() -> None:
    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            command,
            timeout=45,
            output="sensitive command",
            stderr="sensitive error",
        )

    client = DevSQLClient(Path("devsql"), runner=runner)

    with pytest.raises(DevSQLTimeoutError) as error:
        client.query("SELECT source FROM command_events")

    assert "sensitive" not in str(error.value)


def test_query_reports_execution_failure_without_process_output() -> None:
    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        return completed_process(
            stdout="sensitive command",
            stderr="sensitive error",
            return_code=7,
        )

    client = DevSQLClient(Path("devsql"), runner=runner)

    with pytest.raises(DevSQLExecutionError) as error:
        client.query("SELECT source FROM command_events")

    assert error.value.return_code == 7
    assert "sensitive" not in str(error.value)


def test_query_reports_invalid_json_without_including_it() -> None:
    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        return completed_process(stdout="sensitive command is not JSON")

    client = DevSQLClient(Path("devsql"), runner=runner)

    with pytest.raises(DevSQLResponseError) as error:
        client.query("SELECT source FROM command_events")

    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize(
    "response,message",
    [
        ('{"source":"atuin"}', "must be an array"),
        ('["not an object"]', "must be an object"),
    ],
)
def test_query_rejects_invalid_response_shapes(
    response: str,
    message: str,
) -> None:
    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        return completed_process(stdout=response)

    client = DevSQLClient(Path("devsql"), runner=runner)

    with pytest.raises(DevSQLResponseError, match=message):
        client.query("SELECT source FROM command_events")


def test_query_reports_missing_columns_without_including_row_values() -> None:
    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        return completed_process(stdout='[{"command":"sensitive command"}]')

    client = DevSQLClient(Path("devsql"), runner=runner)

    with pytest.raises(DevSQLResponseError) as error:
        client.query(
            "SELECT source FROM command_events",
            required_columns={"source"},
        )

    assert "source" in str(error.value)
    assert "sensitive" not in str(error.value)


def test_query_reports_start_failure_without_executable_path() -> None:
    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("/private/path/devsql: permission denied")

    client = DevSQLClient(Path("/private/path/devsql"), runner=runner)

    with pytest.raises(DevSQLUnavailableError) as error:
        client.query("SELECT source FROM command_events")

    assert "/private/path" not in str(error.value)
