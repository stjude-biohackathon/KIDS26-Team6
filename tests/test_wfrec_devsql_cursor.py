"""Durable cursor behavior for DevSQL-backed shell collection."""

from __future__ import annotations

import json
import stat
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from wfrec.collectors.shell import (
    DEVSQL_CURSOR_SCHEMA_VERSION,
    ShellCollector,
    active_interval_start,
    datetime_to_iso,
    parse_devsql_timestamp,
)
from wfrec.devsql import DevSQLClient
from wfrec.events import SESSION_RESUMED, SHELL_COMMAND, Event
from wfrec.session import Session, SessionStore


def test_cursor_handles_restart_overlap_and_equal_timestamps(
    store: SessionStore,
) -> None:
    session, _ = store.start(title="Cursor", analyst="analyst")
    timestamp = active_interval_start(session)
    first_row = _devsql_command_row(
        source_id="command-1",
        timestamp=timestamp,
    )
    queries: list[str] = []

    first = ShellCollector(
        session,
        devsql_client=_devsql_client([first_row], queries=queries),
    )
    first._run_once()

    second_row = _devsql_command_row(
        source_id="command-2",
        timestamp=timestamp,
    )
    restarted = ShellCollector(
        session,
        devsql_client=_devsql_client(
            [first_row, second_row],
            queries=queries,
        ),
    )
    restarted._run_once()

    events = _shell_events(session)
    assert [event.payload["source_id"] for event in events] == [
        "command-1",
        "command-2",
    ]
    assert all(f"substr(timestamp, 1, 19) >= '{timestamp[:19]}'" in query for query in queries)

    cursor_path = session.root / "shell" / "devsql-cursor.json"
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor == {
        "schema_version": DEVSQL_CURSOR_SCHEMA_VERSION,
        "devsql_version": "devsql 0.5.1",
        "shell_source": "atuin",
        "active_interval_start": timestamp,
        "last_timestamp": timestamp,
        "identities_at_last_timestamp": [
            ["atuin", "shell-session", "command-1"],
            ["atuin", "shell-session", "command-2"],
        ],
    }
    assert stat.S_IMODE(cursor_path.stat().st_mode) == 0o600


def test_cursor_does_not_advance_when_timeline_write_fails(
    store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = store.start(title="Failure", analyst="analyst")
    collector = ShellCollector(
        session,
        devsql_client=_devsql_client(
            [
                _devsql_command_row(
                    source_id="not-written",
                    timestamp=active_interval_start(session),
                )
            ]
        ),
    )

    def fail_extend(events: list[object]) -> list[object]:
        raise OSError("simulated timeline failure")

    monkeypatch.setattr(session.writer, "extend", fail_extend)

    with pytest.raises(OSError, match="simulated timeline failure"):
        collector._run_once()

    cursor_path = session.root / "shell" / "devsql-cursor.json"
    assert not cursor_path.exists()
    assert _shell_events(session) == []


def test_new_active_interval_excludes_commands_from_pause_gap(
    store: SessionStore,
) -> None:
    session, _ = store.start(title="Resume", analyst="analyst")
    first_timestamp = active_interval_start(session)
    first_moment = parse_devsql_timestamp(first_timestamp)
    assert first_moment is not None

    first = ShellCollector(
        session,
        devsql_client=_devsql_client(
            [
                _devsql_command_row(
                    source_id="before-pause",
                    timestamp=first_timestamp,
                )
            ]
        ),
    )
    first._run_once()

    gap_timestamp = datetime_to_iso(first_moment + timedelta(seconds=5))
    resume_timestamp = datetime_to_iso(first_moment + timedelta(seconds=10))
    after_resume_timestamp = datetime_to_iso(first_moment + timedelta(seconds=11))
    session.record(SESSION_RESUMED, ts=resume_timestamp)

    resumed = ShellCollector(
        session,
        devsql_client=_devsql_client(
            [
                _devsql_command_row(
                    source_id="during-pause",
                    timestamp=gap_timestamp,
                ),
                _devsql_command_row(
                    source_id="after-resume",
                    timestamp=after_resume_timestamp,
                ),
            ]
        ),
    )
    resumed._run_once()

    assert [event.payload["source_id"] for event in _shell_events(session)] == [
        "before-pause",
        "after-resume",
    ]
    cursor = json.loads((session.root / "shell" / "devsql-cursor.json").read_text(encoding="utf-8"))
    assert cursor["active_interval_start"] == resume_timestamp


def test_malformed_cursor_recovers_progress_from_timeline(
    store: SessionStore,
) -> None:
    session, _ = store.start(title="Recovery", analyst="analyst")
    timestamp = active_interval_start(session)
    existing_row = _devsql_command_row(
        source_id="existing",
        timestamp=timestamp,
    )

    first = ShellCollector(
        session,
        devsql_client=_devsql_client([existing_row]),
    )
    first._run_once()

    cursor_path = session.root / "shell" / "devsql-cursor.json"
    cursor_path.write_text("{not-json", encoding="utf-8")

    restarted = ShellCollector(
        session,
        devsql_client=_devsql_client(
            [
                existing_row,
                _devsql_command_row(source_id="new", timestamp=timestamp),
            ]
        ),
    )
    restarted._run_once()

    assert [event.payload["source_id"] for event in _shell_events(session)] == [
        "existing",
        "new",
    ]
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor["identities_at_last_timestamp"] == [
        ["atuin", "shell-session", "existing"],
        ["atuin", "shell-session", "new"],
    ]


def test_devsql_version_change_replaces_cursor_metadata(
    store: SessionStore,
) -> None:
    session, _ = store.start(title="Version", analyst="analyst")
    timestamp = active_interval_start(session)
    row = _devsql_command_row(source_id="existing", timestamp=timestamp)

    ShellCollector(
        session,
        devsql_client=_devsql_client([row], version="devsql 0.5.1"),
    )._run_once()
    ShellCollector(
        session,
        devsql_client=_devsql_client([row], version="devsql 0.6.0"),
    )._run_once()

    assert len(_shell_events(session)) == 1
    cursor = json.loads((session.root / "shell" / "devsql-cursor.json").read_text(encoding="utf-8"))
    assert cursor["devsql_version"] == "devsql 0.6.0"


def _devsql_command_row(
    *,
    source_id: str,
    timestamp: str,
) -> dict[str, object]:
    """Build one normalized Atuin row without reading local activity."""

    return {
        "source": "atuin",
        "session_id": "shell-session",
        "source_id": source_id,
        "timestamp": timestamp,
        "duration_ms": 25,
        "command": "python workflow.py",
        "cwd": "/work/project",
        "exit_code": 0,
        "hostname": "workstation",
        "channel": "shell",
        "actor": None,
        "provenance_quality": "explicit",
        "provenance_reason": "test fixture",
        "agent_id": None,
        "agent_role": None,
        "originator": None,
        "tool_name": None,
        "source_path": "",
    }


def _devsql_client(
    rows: list[dict[str, object]],
    *,
    version: str = "devsql 0.5.1",
    queries: list[str] | None = None,
) -> DevSQLClient:
    """Return a deterministic client with separate version/query responses."""

    def runner(
        command: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--version":
            stdout = f"{version}\n"
        else:
            if queries is not None:
                queries.append(command[-1])
            stdout = json.dumps(rows)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    return DevSQLClient(Path("/test/devsql"), runner=runner)


def _shell_events(session: Session) -> list[Event]:
    """Return only collected shell command events."""

    return [event for event in session.writer.read() if event.type == SHELL_COMMAND]
