"""Session lifecycle: the active/paused invariant and mid-session toggles."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest

from wfrec.session import (
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_STOPPED,
    Session,
    SessionNotFound,
    new_session_id,
)
from wfrec.state import RecorderState, read_sentinel, sentinel_enables


def _types(session: Session) -> list[str]:
    return [event.type for event in Session.load(session.session_id).writer.read()]


def test_session_id_combines_sortable_timestamp_and_uuid4():
    session_id = new_session_id()
    timestamp, suffix = session_id.rsplit("_", 1)

    datetime.strptime(timestamp, "%Y-%m-%dT%H-%M-%S")
    assert len(suffix) == 32
    assert UUID(hex=suffix).version == 4


def test_duration_snapshot_includes_current_lifecycle_interval(store) -> None:
    session, _ = store.start(title="A", analyst="a")
    session.manifest.active_seconds = 4.0
    session.manifest.paused_seconds = 2.0
    session.manifest._last_transition = 100.0

    session.manifest.status = STATUS_ACTIVE
    assert session.manifest.duration_snapshot(now=103.5) == (7.5, 2.0)

    session.manifest.status = STATUS_PAUSED
    assert session.manifest.duration_snapshot(now=103.5) == (4.0, 5.5)

    session.manifest.status = STATUS_STOPPED
    assert session.manifest.duration_snapshot(now=103.5) == (4.0, 2.0)


def test_start_creates_folder_manifest_and_timeline(store, wfrec_home):
    session, preempted = store.start(title="HG008 QC", analyst="mgatta42")

    assert preempted is None
    assert session.manifest.status == STATUS_ACTIVE
    assert (session.root / "manifest.json").exists()
    assert (session.root / "events.jsonl").exists()
    assert (session.root / "spool").is_dir()
    assert _types(session)[:2] == ["session.created", "session.started"]


def test_only_one_session_is_ever_active(store):
    """Starting B while A is active must auto-pause A.

    This invariant is what makes the shell hook tractable: it sees one sentinel
    and one spool directory, so there is never an ambiguous owner for a command.
    """

    a, _ = store.start(title="A")
    b, preempted = store.start(title="B")

    assert preempted is not None and preempted.session_id == a.session_id
    assert Session.load(a.session_id).manifest.status == STATUS_PAUSED
    assert Session.load(b.session_id).manifest.status == STATUS_ACTIVE

    actives = [s.session_id for s in store.list_sessions() if s.manifest.status == STATUS_ACTIVE]
    assert actives == [b.session_id]


def test_preemption_is_recorded_on_both_timelines(store):
    """Either timeline alone should explain the handover."""

    a, _ = store.start(title="A")
    b, _ = store.start(title="B")

    a_events = Session.load(a.session_id).writer.read()
    preempted = [e for e in a_events if e.type == "session.preempted"]
    assert preempted and preempted[0].payload["preempted_by"] == b.session_id

    b_events = Session.load(b.session_id).writer.read()
    displaced = [e for e in b_events if e.type == "session.preempted"]
    assert displaced and displaced[0].payload["preempted"] == a.session_id


def test_pause_records_reason_and_clears_sentinel(store):
    session, _ = store.start(title="A")
    store.pause(session.session_id, reason="waiting on alignment", expect="6h")

    assert Session.load(session.session_id).manifest.status == STATUS_PAUSED
    assert read_sentinel() is None, "paused means every open shell stops recording"

    paused = [
        e for e in Session.load(session.session_id).writer.read() if e.type == "session.paused"
    ]
    assert paused[0].payload["reason"] == "waiting on alignment"
    assert paused[0].payload["expect"] == "6h"


def test_resume_from_paused_and_records_gap(store):
    session, _ = store.start(title="A")
    store.pause(session.session_id, reason="x")
    resumed, _ = store.resume(session.session_id)

    assert resumed.manifest.status == STATUS_ACTIVE
    events = [
        e for e in Session.load(session.session_id).writer.read() if e.type == "session.resumed"
    ]
    assert events and "gap_ms" in events[0].payload


def test_resume_while_another_is_active_preempts_it(store):
    a, _ = store.start(title="A")
    store.pause(a.session_id, reason="waiting")
    b, _ = store.start(title="B")

    _resumed, preempted = store.resume(a.session_id)
    assert preempted is not None and preempted.session_id == b.session_id
    assert Session.load(b.session_id).manifest.status == STATUS_PAUSED


def test_stopped_session_cannot_be_resumed(store):
    session, _ = store.start(title="A")
    store.stop(session.session_id)
    assert Session.load(session.session_id).manifest.status == STATUS_STOPPED
    with pytest.raises(ValueError, match="stopped"):
        store.resume(session.session_id)


def test_source_toggle_updates_sentinel_for_open_shells(store):
    """The killer requirement: toggling must reach shells already running.

    The manifest and the sentinel are separate representations; updating only
    the manifest made the UI show a source as off while every open terminal
    kept recording it.
    """

    session, _ = store.start(title="A")
    assert sentinel_enables(read_sentinel()[1], "shell")

    session.set_source("shell", False)
    assert not sentinel_enables(read_sentinel()[1], "shell")

    session.set_source("shell", True)
    assert sentinel_enables(read_sentinel()[1], "shell")


def test_shell_output_rejects_enable_and_clears_legacy_state(store):
    session, _ = store.start(title="A")

    with pytest.raises(ValueError, match="hook-spool"):
        session.set_shell_output(True)

    session.manifest.shell_output = True
    session.save()
    session._sync_state()
    session.set_shell_output(False)

    assert session.manifest.shell_output is False
    assert RecorderState.load().shell_output is False


def test_toggle_on_inactive_session_does_not_touch_live_sentinel(store):
    a, _ = store.start(title="A")
    b, _ = store.start(title="B")  # A is now paused
    before = read_sentinel()[1]

    a.set_source("files", False)

    assert read_sentinel()[1] == before
    assert Session.load(a.session_id).manifest.sources["files"] is False
    assert Session.load(b.session_id).manifest.sources["files"] is True


def test_unknown_source_is_rejected(store):
    session, _ = store.start(title="A")
    with pytest.raises(ValueError, match="Unknown capture source"):
        session.set_source("telepathy", True)


def test_notes_are_redacted_before_they_touch_disk(store):
    """The paste box is the most likely place an identifier lands."""

    session, _ = store.start(title="A")
    event = session.add_note("email bob@stjude.org about SJ001234 MRN 1234567", label="why")

    assert set(event.redactions) >= {"email", "sj_id", "mrn"}
    assert "bob@stjude.org" not in event.payload["text"]

    sidecar = (session.root / event.ref).read_text(encoding="utf-8")
    assert "bob@stjude.org" not in sidecar
    assert "SJ001234" not in sidecar


def test_waiting_heartbeat_distinguishes_a_wait_from_silence(store):
    session, _ = store.start(title="A")
    session.mark_waiting(reason="bwa alignment", elapsed_ms=3600000)
    event = [e for e in session.writer.read() if e.type == "session.waiting"][-1]
    assert event.payload["reason"] == "bwa alignment"
    assert event.payload["elapsed_ms"] == 3600000


def test_sequence_numbers_are_monotonic_and_gapless(store):
    session, _ = store.start(title="A")
    for index in range(25):
        session.add_marker(f"m{index}")
    seqs = [e.seq for e in Session.load(session.session_id).writer.read()]
    assert seqs == list(range(1, len(seqs) + 1))


def test_resolve_falls_back_to_active_then_most_recent(store):
    a, _ = store.start(title="A")
    assert store.resolve(None).session_id == a.session_id
    store.stop(a.session_id)
    # No active session, so the most recent one is the sensible target.
    assert store.resolve(None).session_id == a.session_id


def test_missing_session_raises(store):
    with pytest.raises(SessionNotFound):
        Session.load("2026-01-01T00-00-00_nope")


def test_batched_append_assigns_contiguous_sequence(store):
    from wfrec.events import Event

    session, _ = store.start(title="A")
    before = len(session.writer.read())
    events = [
        Event(source="shell", type="shell.command.completed", payload={"command": f"c{i}"})
        for i in range(5)
    ]
    session.writer.extend(events)
    seqs = [e.seq for e in events]
    assert seqs == list(range(before + 1, before + 6))
