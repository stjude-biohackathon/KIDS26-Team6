"""Crash recovery, the three-phase commit, and the locking property.

Failure is injected **at each journal state**, not by killing a process. Killing
a process gives you one arbitrary interleaving per run and no way to say which
one you got; setting the journal to a state and calling ``recover`` tests the
state machine that actually decides what happens.
"""

from __future__ import annotations

import json
import os
import threading
import hashlib

import pytest

from autocab.recording.events import Event
from autocab.recording.seal import (
    MAX_TAIL_EVENTS,
    MAX_TAIL_RETRIES,
    STATE_COMMITTED,
    STATE_COMMITTING,
    STATE_PLANNING,
    STATE_STAGING,
    SealAborted,
    _commit_staged,
    guard_readers,
    is_sealed,
    journal_path,
    marker_path,
    read_journal,
    recover,
    seal_dir,
    seal_session,
    staged_dir,
    write_journal,
)


@pytest.fixture()
def populated(store):
    session, _ = store.start(title="A", analyst="a")
    session.writer.append(
        Event(
            source="context",
            type="context.note",
            payload={"text": "MRN 4419902 for jane.smith@example.org"},
        )
    )
    (session.root / "jobs").mkdir(parents=True, exist_ok=True)
    (session.root / "jobs" / "1.out").write_text("MRN 4419902\n", encoding="utf-8")
    store.stop(session.session_id)
    return store.resolve(session.session_id)


def _stage_a_fake_seal(session, *, state: str, with_files: bool = True):
    """Build a ``.seal/`` directory by hand, in the given journal state."""

    staged = staged_dir(session.root)
    staged.mkdir(parents=True, exist_ok=True)
    targets: list[str] = []
    record_targets: dict[str, dict[str, str]] = {}
    events = session.root / "events.jsonl"
    manifest = session.root / "manifest.json"
    staged_events = staged / "events.jsonl"
    staged_manifest = staged / "manifest.json"
    staged_deid = staged / "deid"
    staged_deid.mkdir(parents=True, exist_ok=True)
    staged_audit = staged_deid / "audit.jsonl"
    staged_events.write_text(events.read_text(encoding="utf-8"), encoding="utf-8")
    staged_manifest.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
    staged_audit.write_text('{"label":"MRN"}\n', encoding="utf-8")
    targets.extend(["events.jsonl", "manifest.json", "deid/audit.jsonl"])
    if with_files:
        (staged / "jobs").mkdir(parents=True, exist_ok=True)
        (staged / "jobs" / "1.out").write_text("MRN_deadbeefcafe\n", encoding="utf-8")
        targets.append("jobs/1.out")
    for relpath in targets:
        before = session.root / relpath
        after = staged / relpath
        record_targets[relpath] = {
            "before_sha256": (
                hashlib.sha256(before.read_bytes()).hexdigest() if before.exists() else ""
            ),
            "after_sha256": hashlib.sha256(after.read_bytes()).hexdigest(),
        }
    journal = {
        "state": state,
        "session": session.session_id,
        "snapshot_bytes": (session.root / "events.jsonl").stat().st_size,
        "sealed_through_seq": 3,
        "generation": 1,
        "targets": targets,
        "deletions": [],
        "seal_record": {
            "schema": 1,
            "session": session.session_id,
            "assurance": "regex-only",
            "generation": 1,
            "engine": "regex",
            "findings": 1,
            "counts_by_label": {"MRN": 1},
            "distinct_values": 1,
            "targets": record_targets,
            "pseudonym_key": "discarded",
            "reverse_map": "not-written",
            "duration_seconds": 0.0,
        },
    }
    write_journal(session.root, journal)
    return journal


# --------------------------------------------------------------------------
# recovery, per journal state
# --------------------------------------------------------------------------
def test_recovery_from_planning_discards_and_leaves_the_session_unchanged(populated):
    before = (populated.root / "jobs" / "1.out").read_text(encoding="utf-8")
    _stage_a_fake_seal(populated, state=STATE_PLANNING)

    result = recover(populated.root)

    assert result == {"recovered": STATE_PLANNING, "action": "discarded"}
    assert not seal_dir(populated.root).exists()
    assert not is_sealed(populated.root)
    assert (populated.root / "jobs" / "1.out").read_text(encoding="utf-8") == before


def test_recovery_from_staging_discards_and_leaves_the_session_unchanged(populated):
    before = (populated.root / "jobs" / "1.out").read_text(encoding="utf-8")
    _stage_a_fake_seal(populated, state=STATE_STAGING)

    result = recover(populated.root)

    assert result["action"] == "discarded"
    assert not seal_dir(populated.root).exists()
    assert not is_sealed(populated.root)
    assert (populated.root / "jobs" / "1.out").read_text(encoding="utf-8") == before


def test_recovery_from_committing_rolls_forward(populated):
    """Past the point of no return, so finish rather than unwind.

    Half the files may already be replaced and there is no record of which --
    that is precisely what `committing` means -- so the only consistent outcome
    is to complete the commit.
    """

    _stage_a_fake_seal(populated, state=STATE_COMMITTING)

    result = recover(populated.root)

    assert result == {"recovered": STATE_COMMITTING, "action": "rolled-forward"}
    assert (populated.root / "jobs" / "1.out").read_text(encoding="utf-8") == "MRN_deadbeefcafe\n"
    assert is_sealed(populated.root)
    assert not seal_dir(populated.root).exists()


def test_recovery_from_committed_writes_the_marker_and_cleans_up(populated):
    journal = _stage_a_fake_seal(populated, state=STATE_COMMITTING)
    _commit_staged(populated.root, journal)
    journal["state"] = STATE_COMMITTED
    write_journal(populated.root, journal)

    result = recover(populated.root)

    assert result["action"] == "rolled-forward"
    assert is_sealed(populated.root)
    record = json.loads(marker_path(populated.root).read_text(encoding="utf-8"))
    assert record["assurance"] == "regex-only"
    assert not seal_dir(populated.root).exists()


def test_a_torn_journal_is_treated_as_planning(populated):
    """Safe, because the transition to `committing` is fsynced *before* the
    first os.replace -- so a journal too damaged to parse cannot postdate any
    replacement."""

    _stage_a_fake_seal(populated, state=STATE_STAGING)
    journal_path(populated.root).write_text("{not json", encoding="utf-8")

    assert read_journal(populated.root)["state"] == STATE_PLANNING
    recover(populated.root)
    assert not seal_dir(populated.root).exists()
    assert not is_sealed(populated.root)


def test_the_commit_loop_is_idempotent(populated):
    """A missing staged file is safe when its destination has the final digest."""

    journal = _stage_a_fake_seal(populated, state=STATE_COMMITTING)

    first = _commit_staged(populated.root, journal)
    second = _commit_staged(populated.root, journal)
    third = _commit_staged(populated.root, journal)

    assert first == ["events.jsonl", "manifest.json", "deid/audit.jsonl", "jobs/1.out"]
    assert second == [] and third == []
    assert (populated.root / "jobs" / "1.out").read_text(encoding="utf-8") == "MRN_deadbeefcafe\n"


def test_recovery_refuses_a_missing_staged_file_that_was_not_committed(populated):
    _stage_a_fake_seal(populated, state=STATE_COMMITTING)
    (staged_dir(populated.root) / "jobs" / "1.out").unlink()

    with pytest.raises(SealAborted, match="destination jobs/1.out"):
        recover(populated.root)

    assert not marker_path(populated.root).exists()
    assert read_journal(populated.root)["state"] == STATE_COMMITTING


def test_recovery_accepts_a_missing_staged_file_that_was_already_committed(populated):
    _stage_a_fake_seal(populated, state=STATE_COMMITTING)
    source = staged_dir(populated.root) / "jobs" / "1.out"
    os.replace(source, populated.root / "jobs" / "1.out")

    result = recover(populated.root)

    assert result == {"recovered": STATE_COMMITTING, "action": "rolled-forward"}
    assert marker_path(populated.root).exists()
    assert (populated.root / "jobs" / "1.out").read_text(encoding="utf-8") == "MRN_deadbeefcafe\n"


def test_a_half_committed_state_is_never_consumed(populated):
    """A mixed state can exist on disk for milliseconds. Readers refuse."""

    _stage_a_fake_seal(populated, state=STATE_COMMITTING)

    with pytest.raises(Exception) as excinfo:
        guard_readers(populated.root)

    assert "seal in progress" in str(excinfo.value)


def test_recovery_is_re_entrant_safe(populated):
    """Recovery constructs readers, and a reader's own `__init__` calls
    `recover`. Without the guard that is unbounded recursion."""

    _stage_a_fake_seal(populated, state=STATE_COMMITTING)
    recover(populated.root)

    from autocab.recording.session import Session

    reloaded = Session.load(populated.session_id)  # must not recurse
    assert reloaded.writer.sealed


def test_an_interrupted_seal_is_recovered_by_the_next_seal(populated):
    _stage_a_fake_seal(populated, state=STATE_STAGING)

    result = seal_session(populated, force=True, key=b"\x01" * 32)

    assert result.record["generation"] == 1
    assert is_sealed(populated.root)
    assert not seal_dir(populated.root).exists()


def test_an_interrupted_seal_is_recovered_by_the_export_gate(populated):
    _stage_a_fake_seal(populated, state=STATE_COMMITTING)

    from autocab.recording.seal import require_sealed

    record = require_sealed(populated.root, what="test")

    assert record["assurance"] == "regex-only"


# --------------------------------------------------------------------------
# the separate-inode locking property
# --------------------------------------------------------------------------
def test_the_events_lock_is_a_separate_inode_from_the_timeline(populated):
    """**The property the whole seal rests on.**

    ``EventWriter.append`` opens ``events.jsonl`` with ``"a"`` *inside*
    ``file_lock(.events.lock)``, and the lock file is a different inode. So
    ``os.replace(tmp, events.jsonl)`` under that lock is safe: a blocked
    appender re-opens **by path** after the lock releases and lands on the new
    file.

    If someone refactors ``append`` to hold a long-lived append descriptor, the
    seal breaks **silently** -- appends would land in the unlinked old inode and
    vanish with no error anywhere. Hence a direct assertion.
    """

    events = populated.root / "events.jsonl"
    lock = populated.root / ".events.lock"
    populated.writer.append(Event(source="context", type="context.note", payload={"text": "x"}))

    assert lock.exists(), "the lock file must exist independently of the timeline"
    assert events.stat().st_ino != lock.stat().st_ino


def test_the_writer_holds_no_descriptor_across_a_swap(populated):
    """Append, swap the file underneath, append again: both land in the file
    that is at the path now."""

    events = populated.root / "events.jsonl"
    populated.writer.append(Event(source="context", type="context.note", payload={"text": "one"}))
    original_inode = events.stat().st_ino

    replacement = populated.root / "events.jsonl.new"
    replacement.write_text(events.read_text(encoding="utf-8"), encoding="utf-8")
    os.replace(replacement, events)

    assert events.stat().st_ino != original_inode
    populated.writer.append(Event(source="context", type="context.note", payload={"text": "two"}))

    text = events.read_text(encoding="utf-8")
    assert '"two"' in text, "the append landed in the unlinked inode"
    assert '"one"' in text


def test_the_scrub_phase_does_not_hold_the_events_lock(populated):
    """Phase 2 must not block appenders.

    Holding ``.events.lock`` across minutes of inference stalls every collector
    thread in the daemon -- and a safety feature that freezes the recorder is a
    safety feature somebody switches off. Asserted by taking the lock from
    another thread *while* a seal's detection phase is running.
    """

    from autocab.deid.spans import DetectorInfo
    from autocab.recording.locking import file_lock

    acquired = threading.Event()
    release = threading.Event()

    class SlowEngine:
        name = "slow"
        kind = "model"

        def info(self):
            return DetectorInfo(name=self.name, kind=self.kind, available=True)

        def detect(self, texts):
            # Grab .events.lock from another thread mid-detection. If phase 2
            # held it, this would deadlock until the test timed out.
            def grab():
                with file_lock(populated.root / ".events.lock"):
                    acquired.set()
                    release.wait(timeout=5)

            thread = threading.Thread(target=grab, daemon=True)
            thread.start()
            assert acquired.wait(timeout=5), "phase 2 is holding .events.lock"
            release.set()
            thread.join(timeout=5)
            return [[] for _ in texts]

    result = seal_session(populated, detectors=[SlowEngine()], force=True, key=b"\x01" * 32)

    assert acquired.is_set()
    assert is_sealed(populated.root)
    assert result.record["assurance"] == "verified"


# --------------------------------------------------------------------------
# the tail catch-up
# --------------------------------------------------------------------------
def test_a_straggler_appended_after_the_snapshot_is_caught_up(store, monkeypatch):
    """A shell hook's record the daemon ingested a second late.

    Rather than abandoning the whole seal, phase 3 reads the tail past
    ``snapshot_bytes``, scrubs it, appends it to the staged file and retries.
    """

    session, _ = store.start(title="A", analyst="a")
    session.writer.append(
        Event(source="context", type="context.note", payload={"text": "MRN 4419902"})
    )
    store.stop(session.session_id)
    session = store.resolve(session.session_id)

    from autocab.deid.spans import DetectorInfo

    appended = {"done": False}

    class AppendDuringDetection:
        name = "straggler"
        kind = "model"

        def info(self):
            return DetectorInfo(name=self.name, kind=self.kind, available=True)

        def detect(self, texts):
            if not appended["done"]:
                appended["done"] = True
                # Bypass the seal guard the way a live collector would: the
                # session is not sealed yet at this point anyway.
                with (session.root / "events.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "seq": 99,
                                "ts": "2026-09-17T00:00:00.000Z",
                                "session": session.session_id,
                                "host": "h",
                                "analyst": "a",
                                "origin": "local",
                                "source": "shell",
                                "type": "shell.command.completed",
                                "payload": {"command": "grep MRN 4419903 chart.txt"},
                            }
                        )
                        + "\n"
                    )
            return [[] for _ in texts]

    result = seal_session(
        session, detectors=[AppendDuringDetection()], force=True, key=b"\x01" * 32
    )

    text = (session.root / "events.jsonl").read_text(encoding="utf-8")
    assert is_sealed(session.root)
    assert "4419903" not in text, "the straggler was committed unscrubbed"
    assert "4419902" not in text
    assert result.findings >= 2


@pytest.mark.parametrize("bound", ["retries", "events"])
def test_the_tail_catch_up_is_bounded(store, monkeypatch, bound):
    """An unbounded catch-up against a session that is still recording never
    terminates. So both bounds abort with **nothing committed**.

    The bound is driven directly rather than by racing a background appender: a
    timing-dependent version of this test passed or failed depending on how fast
    the retries happened to run, which is the opposite of what a safety test is
    for.
    """

    import autocab.recording.seal as sealmod
    from autocab.deid.spans import DetectorInfo
    from autocab.recording.seal import SealAborted

    session, _ = store.start(title="A", analyst="a")
    session.writer.append(
        Event(source="context", type="context.note", payload={"text": "MRN 4419902"})
    )
    store.stop(session.session_id)
    session = store.resolve(session.session_id)

    events_path = session.root / "events.jsonl"
    before = events_path.read_text(encoding="utf-8")

    if bound == "retries":
        monkeypatch.setattr(sealmod, "MAX_TAIL_RETRIES", 0)
    else:
        monkeypatch.setattr(sealmod, "MAX_TAIL_EVENTS", 0)

    def straggler(_seq: int) -> str:
        return (
            json.dumps(
                {
                    "seq": _seq,
                    "ts": "2026-09-17T00:00:00.000Z",
                    "session": session.session_id,
                    "host": "h",
                    "analyst": "a",
                    "origin": "local",
                    "source": "shell",
                    "type": "shell.command.completed",
                    "payload": {"command": "grep MRN 4419903 chart.txt"},
                }
            )
            + "\n"
        )

    class AppendsForever:
        """Appends on every detection pass, so the size check never settles."""

        name = "never"
        kind = "model"

        def __init__(self) -> None:
            self.seq = 100

        def info(self):
            return DetectorInfo(name=self.name, kind=self.kind, available=True)

        def detect(self, texts):
            with events_path.open("a", encoding="utf-8") as handle:
                handle.write(straggler(self.seq))
            self.seq += 1
            return [[] for _ in texts]

    with pytest.raises(SealAborted) as excinfo:
        seal_session(session, detectors=[AppendsForever()], force=True, key=b"\x01" * 32)

    assert "Nothing was committed" in str(excinfo.value)
    assert not is_sealed(session.root)
    assert not seal_dir(session.root).exists()
    # The session is unchanged: the original text is still there, and the
    # scrubbed version was never committed.
    assert events_path.read_text(encoding="utf-8").startswith(before)
    assert "MRN 4419902" in events_path.read_text(encoding="utf-8")


def test_the_tail_budget_constants_are_finite():
    assert 0 < MAX_TAIL_RETRIES <= 10
    assert 0 < MAX_TAIL_EVENTS <= 1000
