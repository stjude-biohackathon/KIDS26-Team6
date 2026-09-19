"""Agent transcript adapters: normalization, isolation, and the manual path."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterator
from datetime import timedelta
from typing import Any

from autocab.recording.collectors.agents import (
    AgentCollector,
    ClaudeCodeAdapter,
    CursorAdapter,
    DEVSQL_CODEX_COLUMNS,
    DevSQLCodexAdapter,
    Turn,
    _flatten_content,
    _iso,
    attach_transcript,
)
from autocab.recording.collectors.shell import (
    active_interval_start,
    datetime_to_iso,
    parse_devsql_timestamp,
)
from autocab.recording.devsql import DevSQLClient, DevSQLUnavailableError
from autocab.recording.events import AGENT_MESSAGE


class FakeDevSQLClient:
    """Expose deterministic Codex rows while retaining submitted SQL."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def query(
        self,
        sql: str,
        *,
        required_columns: Collection[str] = (),
    ) -> list[dict[str, Any]]:
        self.queries.append(sql)
        if sql.endswith("LIMIT 0"):
            return []
        assert tuple(required_columns) == DEVSQL_CODEX_COLUMNS
        return [dict(row) for row in self.rows]


def _codex_row(
    timestamp: str,
    *,
    record_index: int = 1,
    role: str = "user",
    text: str = "inspect the samples",
) -> dict[str, Any]:
    return {
        "thread_id": "thread-1",
        "record_index": record_index,
        "timestamp": timestamp,
        "role": role,
        "text": text,
        "parent_thread_id": "parent-1",
        "parent_record_index": 8,
        "source_kind": "codex-cli",
        "agent_path": "agents/reviewer",
        "agent_role": "reviewer",
        "originator": "codex",
        "cwd": "/work/project",
        "git_branch": "feature/capture",
    }


def _after(timestamp: str, seconds: int) -> str:
    moment = parse_devsql_timestamp(timestamp)
    assert moment is not None
    return datetime_to_iso(moment + timedelta(seconds=seconds))


def test_flatten_handles_every_content_shape():
    assert _flatten_content("plain") == "plain"
    assert (
        _flatten_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    )
    assert "tool_use Bash" in _flatten_content(
        [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]
    )
    assert _flatten_content(None) == ""


def test_iso_normalizes_epoch_millis_and_iso_strings():
    assert _iso(1789578668017).endswith("Z")
    assert _iso("2026-09-16T17:11:08.017Z") == "2026-09-16T17:11:08.017Z"
    assert _iso("nonsense") is None
    assert _iso(None) is None


def test_claude_adapter_reads_jsonl_and_tails_by_offset(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "-tmp-proj"
    root.mkdir(parents=True)
    transcript = root / "session.jsonl"

    def write(records):
        with transcript.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    write(
        [
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-09-16T17:00:00Z",
                "cwd": "/tmp/proj",
                "version": "2.1.246",
                "message": {"content": "run the qc"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-09-16T17:00:05Z",
                "cwd": "/tmp/proj",
                "version": "2.1.246",
                "message": {"content": [{"type": "text", "text": "running it"}]},
            },
            {"type": "atis-latch", "uuid": "x1", "timestamp": "2026-09-16T17:00:06Z"},
            {"type": "user", "uuid": "m1", "isMeta": True, "message": {"content": "meta noise"}},
        ]
    )

    adapter = ClaudeCodeAdapter()
    adapter.root = tmp_path / "projects"

    turns = list(adapter.turns(0.0))
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].text == "run the qc"
    assert turns[0].meta["version"] == "2.1.246"

    # Undocumented record types must be skipped, not raise.
    assert all(t.role in {"user", "assistant"} for t in turns)

    # Offset tailing: nothing new until the file grows.
    assert list(adapter.turns(0.0)) == []
    write(
        [
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "2026-09-16T17:01:00Z",
                "message": {"content": "and the benchmark"},
            }
        ]
    )
    assert [t.text for t in adapter.turns(0.0)] == ["and the benchmark"]


def test_claude_adapter_seek_to_end_skips_existing_history(tmp_path):
    """Pressing record must not ingest an hour of prior conversation."""

    root = tmp_path / "projects" / "-p"
    root.mkdir(parents=True)
    transcript = root / "s.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps({"type": "user", "uuid": f"u{i}", "message": {"content": f"old {i}"}})
            for i in range(50)
        )
        + "\n",
        encoding="utf-8",
    )

    adapter = ClaudeCodeAdapter()
    adapter.root = tmp_path / "projects"
    adapter.seek_to_end()

    assert list(adapter.turns(0.0)) == []

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "user", "uuid": "new", "message": {"content": "fresh"}}) + "\n"
        )
    assert [t.text for t in adapter.turns(0.0)] == ["fresh"]


def test_cursor_adapter_opens_sqlite_read_only(tmp_path):
    """Cursor holds the DB open in WAL mode; we must never lock it."""

    database = tmp_path / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
    connection.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:c1:b1", json.dumps({"type": 1, "text": "why is this BAM truncated"})),
    )
    connection.commit()
    connection.close()

    adapter = CursorAdapter()
    adapter.databases = [database]
    turns = list(adapter.turns(0.0))

    assert len(turns) == 1
    assert turns[0].role == "user"
    assert turns[0].text == "why is this BAM truncated"

    # Confirm read-only: writing through the adapter's own connection must fail.
    read_only = adapter._connect(database)
    try:
        with __import__("pytest").raises(sqlite3.OperationalError):
            read_only.execute("INSERT INTO cursorDiskKV VALUES ('x','y')")
    finally:
        read_only.close()


def test_devsql_codex_adapter_joins_normalized_message_metadata(
    store: Any,
) -> None:
    session, _ = store.start(title="A", analyst="a")
    interval_start = active_interval_start(session)
    client = FakeDevSQLClient(
        [
            _codex_row(interval_start, record_index=4),
            _codex_row(
                _after(interval_start, 1),
                record_index=5,
                role="assistant",
                text="The sample sheet is valid.",
            ),
        ]
    )
    adapter = DevSQLCodexAdapter(client, since=interval_start)

    assert adapter.available() is True
    turns = list(adapter.turns(0.0))

    assert [turn.role for turn in turns] == ["user", "assistant"]
    assert turns[0].key == "codex:thread-1:4"
    assert turns[0].cwd == "/work/project"
    assert turns[0].meta == {
        "provider": "devsql",
        "session_id": "thread-1",
        "source_id": "4",
        "parent_session_id": "parent-1",
        "parent_record_index": 8,
        "source_kind": "codex-cli",
        "agent_id": "agents/reviewer",
        "agent_role": "reviewer",
        "originator": "codex",
        "git_branch": "feature/capture",
    }
    assert "JOIN codex_threads AS thread" in client.queries[0]
    assert client.queries[0].endswith("LIMIT 0")
    assert "message.is_canonical = 1" in client.queries[1]
    assert "message.role IN ('user', 'assistant')" in client.queries[1]
    assert "command_events" not in client.queries[1]


def test_collector_captures_claude_and_codex_together(store: Any, monkeypatch: Any) -> None:
    session, _ = store.start(title="A", analyst="a")
    interval_start = active_interval_start(session)
    client = FakeDevSQLClient([_codex_row(interval_start)])

    monkeypatch.setattr(ClaudeCodeAdapter, "available", lambda self: True)
    monkeypatch.setattr(ClaudeCodeAdapter, "seek_to_end", lambda self: None)

    def claude_turns(self: ClaudeCodeAdapter, since: float) -> Iterator[Turn]:
        yield Turn(
            "claude-code",
            "assistant",
            "Claude is still captured.",
            key="claude:a1",
        )

    monkeypatch.setattr(ClaudeCodeAdapter, "turns", claude_turns)
    collector = AgentCollector(session, devsql_client=client)

    status = collector.probe()
    collector._run_once()

    messages = [event for event in session.writer.read() if event.type == AGENT_MESSAGE]
    assert status.backend.startswith("devsql-codex,claude-code")
    assert {event.payload["tool"] for event in messages} == {
        "claude-code",
        "codex",
    }


def test_devsql_probe_failure_keeps_direct_claude(store: Any, monkeypatch: Any) -> None:
    session, _ = store.start(title="A", analyst="a")
    client = FakeDevSQLClient([])

    def fail_query(sql: str, *, required_columns: Collection[str] = ()) -> list[dict[str, Any]]:
        raise RuntimeError("schema changed")

    client.query = fail_query
    monkeypatch.setattr(ClaudeCodeAdapter, "available", lambda self: True)
    monkeypatch.setattr(ClaudeCodeAdapter, "seek_to_end", lambda self: None)

    status = AgentCollector(session, devsql_client=client).probe()

    assert status.available is True
    assert "claude-code" in status.backend.split(",")
    assert "devsql-codex" not in status.backend.split(",")
    assert status.extra["detected"]["devsql-codex"] is False


def test_devsql_codex_dedupes_across_polls_and_restart(
    store: Any,
) -> None:
    session, _ = store.start(title="A", analyst="a")
    interval_start = active_interval_start(session)
    event_time = _after(interval_start, 5)
    client = FakeDevSQLClient([_codex_row(event_time)])

    first = AgentCollector(session)
    first._adapters = [DevSQLCodexAdapter(client, since=interval_start)]
    first._run_once()
    first._run_once()

    restarted = AgentCollector(session)
    restarted._adapters = [
        DevSQLCodexAdapter(
            client,
            since=interval_start,
            existing_events=session.writer.read(),
        )
    ]
    restarted._run_once()

    messages = [event for event in session.writer.read() if event.type == AGENT_MESSAGE]
    assert len(messages) == 1
    assert messages[0].payload["capture_id"] == "codex:thread-1:1"
    assert event_time[:19] in client.queries[-1]


def test_devsql_codex_excludes_messages_before_active_interval(
    store: Any,
) -> None:
    session, _ = store.start(title="A", analyst="a")
    interval_start = active_interval_start(session)
    client = FakeDevSQLClient(
        [
            _codex_row(_after(interval_start, -1), record_index=1),
            _codex_row(_after(interval_start, 1), record_index=2),
        ]
    )
    adapter = DevSQLCodexAdapter(client, since=interval_start)

    turns = list(adapter.turns(0.0))

    assert [turn.key for turn in turns] == ["codex:thread-1:2"]


def test_one_failing_adapter_does_not_stop_the_others(store):
    session, _ = store.start(title="A", analyst="a")
    collector = AgentCollector(session)

    class Broken:
        name = "broken"

        def available(self):
            return True

        def seek_to_end(self):
            pass

        def turns(self, since):
            raise RuntimeError("schema changed")

    class Working:
        name = "working"

        def available(self):
            return True

        def seek_to_end(self):
            pass

        def turns(self, since):
            yield Turn("working", "user", "still here", key="k1")

    collector._adapters = [Broken(), Working()]
    collector._run_once()

    events = session.writer.read()
    failures = [e for e in events if e.type == "agent.adapter.failed"]
    messages = [e for e in events if e.type == "agent.message"]
    assert failures and failures[0].payload["tool"] == "broken"
    assert messages and messages[0].payload["text"] == "still here"

    # The broken adapter is quarantined, not retried forever.
    collector._run_once()
    assert len([e for e in session.writer.read() if e.type == "agent.adapter.failed"]) == 1
    assert collector.status.extra["failed"] == ["broken"]


def test_turns_are_deduped_and_redacted(store):
    session, _ = store.start(title="A", analyst="a")
    collector = AgentCollector(session)
    turn = Turn("claude-code", "user", "mail bob@stjude.org re SJ001234", key="dup")

    first = collector._to_event(turn)
    second = collector._to_event(turn)

    assert first is not None and second is None
    assert "bob@stjude.org" not in first.payload["text"]
    assert set(first.redactions) >= {"email", "sj_id"}


def test_very_long_turn_is_truncated_head_and_tail(store):
    session, _ = store.start(title="A", analyst="a")
    collector = AgentCollector(session)
    event = collector._to_event(Turn("t", "assistant", "A" * 30000, key="long"))
    assert event.payload["truncated"] is True
    assert "elided by wfrec" in event.payload["text"]
    assert event.payload["chars"] == 30000


def test_attach_transcript_handles_jsonl_json_and_plain_text(store, tmp_path):
    """The universal fallback: must work whatever the file actually is."""

    session, _ = store.start(title="A", analyst="a")

    jsonl = tmp_path / "a.jsonl"
    jsonl.write_text(
        json.dumps({"role": "user", "content": "first"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "second"})
        + "\n",
        encoding="utf-8",
    )
    assert len(attach_transcript(session, jsonl, tool="copilot")) == 2

    plain = tmp_path / "b.txt"
    plain.write_text("just some notes about the run", encoding="utf-8")
    events = attach_transcript(session, plain, tool="manual")
    assert len(events) == 1
    assert "just some notes" in events[0].payload["text"]


def test_no_agent_tools_degrades_with_manual_hint(store, monkeypatch, tmp_path):
    session, _ = store.start(title="A", analyst="a")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "empty")

    def missing_devsql() -> DevSQLClient:
        raise DevSQLUnavailableError("DevSQL executable is not available.")

    status = AgentCollector(session, devsql_discover=missing_devsql).safe_probe()
    assert status.available is False
    assert "attach-transcript" in status.detail
