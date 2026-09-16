"""Agent transcript adapters: normalization, isolation, and the manual path."""

from __future__ import annotations

import json
import sqlite3

from wfrec.collectors.agents import (
    AgentCollector,
    ClaudeCodeAdapter,
    CursorAdapter,
    Turn,
    _flatten_content,
    _iso,
    attach_transcript,
)


def test_flatten_handles_every_content_shape():
    assert _flatten_content("plain") == "plain"
    assert _flatten_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert "tool_use Bash" in _flatten_content([{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}])
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

    write([
        {"type": "user", "uuid": "u1", "timestamp": "2026-09-16T17:00:00Z", "cwd": "/tmp/proj",
         "version": "2.1.246", "message": {"content": "run the qc"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-16T17:00:05Z", "cwd": "/tmp/proj",
         "version": "2.1.246", "message": {"content": [{"type": "text", "text": "running it"}]}},
        {"type": "atis-latch", "uuid": "x1", "timestamp": "2026-09-16T17:00:06Z"},
        {"type": "user", "uuid": "m1", "isMeta": True, "message": {"content": "meta noise"}},
    ])

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
    write([{"type": "user", "uuid": "u2", "timestamp": "2026-09-16T17:01:00Z",
            "message": {"content": "and the benchmark"}}])
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
        handle.write(json.dumps({"type": "user", "uuid": "new", "message": {"content": "fresh"}}) + "\n")
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
        json.dumps({"role": "user", "content": "first"}) + "\n"
        + json.dumps({"role": "assistant", "content": "second"}) + "\n",
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
    status = AgentCollector(session).safe_probe()
    assert status.available is False
    assert "attach-transcript" in status.detail
