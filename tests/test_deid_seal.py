"""The seal: mechanics, preconditions, gates, and the marker.

Uses a fake engine where a model tier is needed, so every mechanic here is
verified at zero model cost -- which is the point of keeping detection in
``autocab.deid`` and orchestration in ``wfrec.seal``.
"""

from __future__ import annotations

import json

import pytest

from autocab.deid.policy import Policy, RenderMode
from autocab.deid.spans import DetectorInfo, Span
from wfrec.events import DEID_SEALED, Event, SessionSealed
from wfrec.seal import (
    PATH_KEYS,
    SKIP_KEYS,
    NotSealed,
    SealInProgress,
    SessionActive,
    is_sealed,
    marker_path,
    read_journal,
    recover,
    require_sealed,
    seal_session,
    seal_status,
    transform_strings,
)


class FakeEngine:
    """A model tier that finds one fixed word. No weights, no download.

    Exists so the three-phase commit, the journal, the audit and the reseal
    path can all be tested without the packaged model -- and so the *absence*
    of a model tier cannot silently become the only thing those tests cover.
    """

    name = "fake_model"
    kind = "model"

    def __init__(self, needle: str = "Zephyrine", label: str = "NAME") -> None:
        self.needle = needle
        self.label = label
        self.batches: list[int] = []

    def info(self) -> DetectorInfo:
        return DetectorInfo(
            name=self.name,
            kind=self.kind,
            available=True,
            version="fake-1",
            detail={"weights_sha256": "0" * 64, "revision": "fake"},
        )

    def detect(self, texts):
        # Recording the batch sizes is how `test_detection_is_batched` proves
        # the seal is not invoking a model once per string.
        self.batches.append(len(texts))
        out = []
        for text in texts:
            spans = []
            start = text.find(self.needle)
            while start != -1:
                spans.append(
                    Span(
                        start=start,
                        end=start + len(self.needle),
                        label=self.label,
                        detector=f"model:{self.name}",
                        score=0.97,
                    )
                )
                start = text.find(self.needle, start + 1)
            out.append(spans)
        return out


@pytest.fixture()
def unsealed(store):
    """A stopped session with a distinct sentinel in a variety of fields."""

    session, _ = store.start(title="deid", analyst="analyst-a")
    session.writer.append(
        Event(
            source="context",
            type="context.note",
            payload={
                "text": "Patient: Zephyrine Quibblewick, MRN 4419902, DOB 2012-06-01",
                "label": "why",
            },
        )
    )
    session.writer.append(
        Event(
            source="shell",
            type="shell.command.completed",
            payload={
                "command": "scp jane.smith@example.org:/x .",
                "cwd": "/data/proj/SJALL018",
                "exit_code": 0,
                "duration_ms": 12,
            },
        )
    )
    (session.root / "jobs").mkdir(parents=True, exist_ok=True)
    (session.root / "jobs" / "412391.out").write_text(
        "Comment MRN 4419902 / Zephyrine Quibblewick\nRef GRCh38 SRR12345678\n",
        encoding="utf-8",
    )
    (session.root / "files" / "diffs").mkdir(parents=True, exist_ok=True)
    (session.root / "files" / "diffs" / "a.patch").write_text(
        "+contact: jane.smith@example.org\n+ref: GRCh38\n", encoding="utf-8"
    )
    (session.root / "shell" / "remote").mkdir(parents=True, exist_ok=True)
    (session.root / "shell" / "remote" / "hpc.log").write_text(
        "$ grep MRN 4419902 chart.txt\n", encoding="utf-8"
    )
    store.stop(session.session_id)
    return store.resolve(session.session_id)


# --------------------------------------------------------------------------
# the payload walker
# --------------------------------------------------------------------------
def test_the_walker_scrubs_every_string_it_has_not_been_told_to_skip():
    """Inverted schema rule: adding an event type can only over-scrub.

    A fixed field map would let a new payload key bypass the seal silently,
    which is leak-by-default.
    """

    seen: list[str] = []
    payload = {
        "text": "a",
        "nested": {"deep": {"deeper": "b"}},
        "items": [{"x": "c"}, "d"],
        "seq": 7,
        "exit_code": 0,
        "count": 3,
    }
    transform_strings(payload, lambda path, text, _is_path: seen.append(path) or text)

    assert set(seen) == {"text", "nested.deep.deeper", "items[0].x", "items[1]"}


def test_structural_keys_are_left_alone():
    payload = {key: "SENTINEL" for key in sorted(SKIP_KEYS)}
    out = transform_strings(payload, lambda _p, _t, _i: "SCRUBBED")

    assert out == payload


def test_workforce_keys_are_kept_by_default_and_scrubbed_on_request():
    """`analyst` identifies the workforce, not the PHI subject -- and
    `WorkflowClusterer` groups on it."""

    payload = {"analyst": "analyst-a", "host": "node07", "text": "x"}

    kept = transform_strings(payload, lambda _p, _t, _i: "SCRUBBED")
    assert kept["analyst"] == "analyst-a" and kept["host"] == "node07"
    assert kept["text"] == "SCRUBBED"

    scrubbed = transform_strings(
        payload, lambda _p, _t, _i: "SCRUBBED", skip_workforce=False
    )
    assert scrubbed["analyst"] == "SCRUBBED"


def test_path_fields_are_flagged_for_component_wise_scrubbing():
    flags: dict[str, bool] = {}
    payload = {key: "/a/b" for key in sorted(PATH_KEYS)} | {"text": "/a/b"}
    transform_strings(
        payload, lambda path, text, is_path: flags.__setitem__(path, is_path) or text
    )

    assert all(flags[key] for key in PATH_KEYS)
    assert flags["text"] is False


def test_paths_scrub_component_wise_keeping_separators(unsealed):
    seal_session(unsealed, force=True, key=b"\x01" * 32)
    events = [
        json.loads(line)
        for line in (unsealed.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    cwd = next(
        event["payload"]["cwd"]
        for event in events
        if event["type"] == "shell.command.completed"
    )

    assert cwd.startswith("/data/proj/")
    assert cwd.count("/") == 3
    assert "SJALL018" not in cwd
    assert cwd.rsplit("/", 1)[-1].startswith("SUBJ_")


# --------------------------------------------------------------------------
# preconditions
# --------------------------------------------------------------------------
def test_an_active_session_is_refused_rather_than_raced(store):
    session, _ = store.start(title="A", analyst="a")
    session.writer.append(Event(source="context", type="context.note", payload={"text": "x"}))

    with pytest.raises(SessionActive) as excinfo:
        seal_session(session, key=b"\x01" * 32)

    assert "not idle" in str(excinfo.value)
    # All three checks are named, because any one of them can be stale.
    assert "RecorderState" in str(excinfo.value)
    assert "sentinel" in str(excinfo.value)
    assert "manifest.status" in str(excinfo.value)
    assert not is_sealed(session.root)


def test_force_stops_the_session_first_rather_than_sealing_anyway(store):
    session, _ = store.start(title="A", analyst="a")
    session.writer.append(
        Event(source="context", type="context.note", payload={"text": "MRN 4419902"})
    )

    seal_session(
        session,
        force=True,
        key=b"\x01" * 32,
        stop_session=lambda: store.stop(session.session_id),
    )

    from wfrec.session import STATUS_STOPPED

    assert store.resolve(session.session_id).manifest.status == STATUS_STOPPED
    assert is_sealed(session.root)


# --------------------------------------------------------------------------
# the seal record
# --------------------------------------------------------------------------
def test_seal_writes_the_marker_and_the_integrity_chain(unsealed):
    result = seal_session(unsealed, force=True, engine_label="regex", key=b"\x01" * 32)
    record = json.loads(marker_path(unsealed.root).read_text(encoding="utf-8"))

    assert record == result.record
    assert record["assurance"] == "regex-only"
    assert record["generation"] == 1
    assert record["pseudonym_key"] == "discarded"
    assert record["reverse_map"] == "not-written"
    assert record["findings"] > 0
    assert record["sealed_through_seq"] > 0
    # Per-target before/after digests over every file the seal rewrote.
    for name, digests in record["targets"].items():
        assert len(digests["after_sha256"]) == 64, name
    assert "events.jsonl" in record["targets"]
    assert "deid/audit.jsonl" in record["targets"]
    # The regex tier's pattern digest is part of the chain.
    regex = next(info for info in record["engines"] if info["name"] == "regex")
    assert len(regex["patterns_sha256"]) == 64


def test_a_model_tier_makes_the_assurance_verified(unsealed):
    engine = FakeEngine()
    result = seal_session(
        unsealed, detectors=[engine], force=True, engine_label="gliner", key=b"\x01" * 32
    )

    assert result.record["assurance"] == "verified"
    assert any(info["name"] == "fake_model" for info in result.record["engines"])
    assert result.record["counts_by_label"].get("NAME", 0) >= 1


def test_a_degraded_run_is_partial_and_never_verified(unsealed):
    result = seal_session(
        unsealed,
        detectors=[FakeEngine()],
        force=True,
        degraded=True,
        degraded_reasons=["llm unreachable"],
        key=b"\x01" * 32,
    )

    assert result.record["assurance"] == "partial"
    assert result.record["degraded_reasons"] == ["llm unreachable"]


def test_a_partial_seal_is_refused_by_the_export_gate(unsealed):
    seal_session(unsealed, force=True, degraded=True, key=b"\x01" * 32)

    with pytest.raises(NotSealed, match="assurance is `partial`"):
        require_sealed(unsealed.root, what="test")


def test_the_export_gate_rejects_post_seal_tampering(unsealed):
    seal_session(unsealed, force=True, key=b"\x01" * 32)
    (unsealed.root / "events.jsonl").write_text(
        (unsealed.root / "events.jsonl").read_text(encoding="utf-8") + "{}\n",
        encoding="utf-8",
    )

    with pytest.raises(NotSealed, match="sealed digest"):
        require_sealed(unsealed.root, what="test")


def test_the_timeline_self_documents_that_it_was_sealed(unsealed):
    seal_session(unsealed, force=True, key=b"\x01" * 32)
    lines = (unsealed.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])

    assert last["type"] == DEID_SEALED
    assert last["payload"]["generation"] == 1
    # It is the LAST line, before hashing, so `after_sha256` covers it.
    record = json.loads(marker_path(unsealed.root).read_text(encoding="utf-8"))
    import hashlib

    digest = hashlib.sha256((unsealed.root / "events.jsonl").read_bytes()).hexdigest()
    assert record["targets"]["events.jsonl"]["after_sha256"] == digest


def test_manifest_is_sealed_alongside_the_timeline(unsealed):
    unsealed.manifest.title = "MRN 4419902 analysis"
    unsealed.manifest.tags = ["SJ-4817"]
    unsealed.manifest.watch_roots = ["/data/SJ-4817"]
    unsealed.save()

    seal_session(unsealed, force=True, key=b"\x01" * 32)

    manifest = json.loads((unsealed.root / "manifest.json").read_text(encoding="utf-8"))
    assert "4419902" not in manifest["title"]
    assert "SJ-4817" not in json.dumps(manifest)
    assert "manifest.json" in json.loads(marker_path(unsealed.root).read_text(encoding="utf-8"))["targets"]


def test_pseudonymize_analyst_scrubs_event_metadata_and_manifest(unsealed):
    unsealed.manifest.analyst = "Jane Smith"
    unsealed.manifest.host = "SJ-4817-host"
    unsealed.save()

    seal_session(
        unsealed,
        force=True,
        key=b"\x01" * 32,
        policy=Policy(render=RenderMode.PSEUDONYMIZE, pseudonymize_analyst=True),
    )

    timeline = (unsealed.root / "events.jsonl").read_text(encoding="utf-8")
    manifest = (unsealed.root / "manifest.json").read_text(encoding="utf-8")
    assert "Jane Smith" not in timeline
    assert "SJ-4817-host" not in timeline
    assert "Jane Smith" not in manifest
    assert "SJ-4817-host" not in manifest


def test_the_sealed_event_does_not_become_a_workflow_step(unsealed):
    seal_session(unsealed, force=True, key=b"\x01" * 32)

    from wfrec.exporters.trace import build_trace

    trace, _steps = build_trace(unsealed)

    assert DEID_SEALED not in json.dumps(trace)
    assert all(step.get("tool") != "deid" for step in trace["steps"])


def test_the_seal_bumps_the_seq_counter_it_consumed(unsealed):
    result = seal_session(unsealed, force=True, key=b"\x01" * 32)
    counter = int((unsealed.root / ".seq").read_text(encoding="utf-8").strip())

    assert counter >= result.record["sealed_through_seq"] + 1


# --------------------------------------------------------------------------
# marker, reseal, dry run
# --------------------------------------------------------------------------
def test_sealing_twice_is_a_no_op_without_reseal(unsealed):
    first = seal_session(unsealed, force=True, key=b"\x01" * 32)
    before = (unsealed.root / "events.jsonl").read_bytes()

    second = seal_session(unsealed, force=True, key=b"\x01" * 32)

    assert second.record["generation"] == first.record["generation"] == 1
    assert second.findings == 0
    assert (unsealed.root / "events.jsonl").read_bytes() == before


def test_reseal_mints_generation_two_only_for_what_was_missed(unsealed):
    seal_session(unsealed, force=True, engine_label="regex", key=b"\x01" * 32)
    # The regex tier catches `Patient: Zephyrine ...` (a labelled-name rule) but
    # not the bare occurrence in the scheduler output -- which is exactly the
    # NAME 0.37 the eval harness measures, and exactly what the model tier is
    # for.
    jobs = (unsealed.root / "jobs" / "412391.out").read_text(encoding="utf-8")
    assert "Zephyrine" in jobs, "a bare name is not regex-detectable"

    engine = FakeEngine()
    second = seal_session(
        unsealed,
        detectors=[engine],
        force=True,
        reseal=True,
        engine_label="gliner",
        key=b"\x02" * 32,
    )
    text_after = (unsealed.root / "events.jsonl").read_text(encoding="utf-8")
    jobs_after = (unsealed.root / "jobs" / "412391.out").read_text(encoding="utf-8")

    assert second.record["generation"] == 2
    assert "Zephyrine" not in jobs_after
    assert "Zephyrine" not in text_after
    # Generation-1 surrogates are untouched: SurrogateGuard protects them, so
    # they show up as masked-at-capture rather than being re-pseudonymized.
    assert second.record["masked_at_capture"] > 0
    assert "MRN_" in text_after


def test_a_dry_run_reports_real_numbers_and_touches_nothing(unsealed):
    before = (unsealed.root / "events.jsonl").read_bytes()

    result = seal_session(unsealed, force=True, dry_run=True, key=b"\x01" * 32)

    assert result.dry_run
    assert result.findings > 0
    assert (unsealed.root / "events.jsonl").read_bytes() == before
    assert not is_sealed(unsealed.root)
    assert read_journal(unsealed.root) is None
    assert not (unsealed.root / ".seal").exists()


def test_seal_status_never_mutates(unsealed):
    assert seal_status(unsealed.root)["sealed"] is False
    seal_session(unsealed, force=True, key=b"\x01" * 32)
    status = seal_status(unsealed.root)

    assert status["sealed"] is True
    assert status["seal"]["assurance"] == "regex-only"
    assert status["journal"] is None


# --------------------------------------------------------------------------
# post-seal immutability
# --------------------------------------------------------------------------
def test_a_sealed_session_refuses_appends(unsealed):
    seal_session(unsealed, force=True, key=b"\x01" * 32)

    from wfrec.session import Session

    reloaded = Session.load(unsealed.session_id)
    assert reloaded.writer.sealed

    with pytest.raises(SessionSealed):
        reloaded.writer.append(
            Event(source="context", type="context.note", payload={"text": "MRN 4419903"})
        )
    with pytest.raises(SessionSealed):
        reloaded.writer.extend(
            [Event(source="context", type="context.note", payload={"text": "MRN 4419903"})]
        )


def test_a_writer_loaded_before_the_seal_still_refuses_new_appends(unsealed):
    stale = unsealed.writer
    seal_session(unsealed, force=True, key=b"\x01" * 32)

    with pytest.raises(SessionSealed):
        stale.append(Event(source="context", type="context.note", payload={"text": "MRN 4419903"}))


def test_the_seal_itself_can_still_write_through_the_bypass(unsealed):
    seal_session(unsealed, force=True, key=b"\x01" * 32)

    from wfrec.events import EventWriter

    writer = EventWriter(
        unsealed.root, session_id=unsealed.session_id, allow_sealed=True
    )
    writer.append(Event(source="deid", type="deid.sealed", payload={"generation": 9}))

    assert "\"generation\": 9" in (unsealed.root / "events.jsonl").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# batching
# --------------------------------------------------------------------------
def test_detection_is_batched_not_per_string(unsealed):
    """`Detector.detect` is batch-shaped by contract for exactly this reason.

    With a per-string call site, a model across 30k events turns a 40-second
    seal into an hour.
    """

    engine = FakeEngine()
    seal_session(unsealed, detectors=[engine], force=True, key=b"\x01" * 32)

    assert len(engine.batches) == 1, f"one batched call expected, got {engine.batches}"
    assert engine.batches[0] > 1


def test_out_of_bounds_detector_offsets_abort_the_seal(store):
    session, _ = store.start(title="A", analyst="a")
    session.writer.append(
        Event(source="context", type="context.note", payload={"text": "MRN 4419902"})
    )
    store.stop(session.session_id)
    session = store.resolve(session.session_id)

    class BadOffsets:
        name = "bad"
        kind = "model"

        def info(self):
            return DetectorInfo(name=self.name, kind=self.kind, available=True)

        def detect(self, texts):
            return [[Span(start=-1, end=3, label="MRN", detector="model:bad")] for _ in texts]

    with pytest.raises(Exception, match="outside text of length"):
        seal_session(session, detectors=[BadOffsets()], force=True, key=b"\x01" * 32)


def test_repeated_identical_strings_are_counted_once(unsealed):
    unsealed.writer.append(
        Event(
            source="context",
            type="context.note",
            payload={"text": "Patient: Zephyrine Quibblewick, MRN 4419902, DOB 2012-06-01"},
        )
    )

    result = seal_session(unsealed, force=True, key=b"\x01" * 32)

    assert result.record["findings"] < 20


def test_identical_strings_are_detected_once(store):
    """Consecutive OCR frames share most of their text."""

    session, _ = store.start(title="A", analyst="a")
    session.writer.extend(
        [
            Event(source="screen", type="screen.ocr", payload={"ocr_text": "MRN 4419902"})
            for _ in range(50)
        ]
    )
    store.stop(session.session_id)
    session = store.resolve(session.session_id)
    engine = FakeEngine()

    result = seal_session(session, detectors=[engine], force=True, key=b"\x01" * 32)

    # 50 events, one distinct string among them.
    assert engine.batches[0] < 10
    assert result.record["distinct_values"] == 1
    assert result.record["counts_by_label"]["MRN"] == 1


# --------------------------------------------------------------------------
# recovery
# --------------------------------------------------------------------------
def test_recover_is_a_no_op_with_no_journal(unsealed):
    assert recover(unsealed.root) is None


def test_readers_refuse_while_a_journal_sits_in_committing(unsealed):
    """A mixed state can exist on disk for milliseconds; it is never consumed."""

    from wfrec.seal import guard_readers, write_journal

    write_journal(
        unsealed.root,
        {"state": "committing", "targets": [], "deletions": [], "sealed_through_seq": 0},
    )
    # `recover` rolls this forward (nothing staged), so guard_readers passes
    # afterwards -- but a journal it cannot finish must block.
    write_journal(
        unsealed.root,
        {"state": "committing", "targets": [], "deletions": [], "sealed_through_seq": 0},
    )
    with pytest.raises(SealInProgress):
        guard_readers(unsealed.root)


# --------------------------------------------------------------------------
# target coverage
# --------------------------------------------------------------------------
def test_nested_remote_files_are_sealed(store):
    """`pull_spool` writes to `shell/remote/<host>/<file>`, not `shell/remote/<file>`.

    A single-level `shell/remote/*` glob matches only the host *directory*,
    which `is_file()` discards -- so every pulled remote file went unsealed and
    nothing complained. The glob that looks obviously right is the one that
    silently covers nothing.
    """

    session, _ = store.start(title="A", analyst="a")
    nested = session.root / "shell" / "remote" / "hpc-login"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "host-1.rec").write_text("$ grep 'MRN 4419902' chart.txt\n", encoding="utf-8")
    store.stop(session.session_id)
    session = store.resolve(session.session_id)

    result = seal_session(session, force=True, key=b"\x01" * 32)

    assert "shell/remote/hpc-login/host-1.rec" in result.record["targets"]
    assert "4419902" not in (nested / "host-1.rec").read_text(encoding="utf-8")


def test_every_documented_target_channel_is_actually_reached(store):
    """One file per covered channel, each with a planted identifier."""

    from wfrec.seal import iter_text_targets

    session, _ = store.start(title="A", analyst="a")
    files = {
        "context/note-1.md": "MRN 4419902\n",
        "screen/ocr/frame-1.txt": "MRN 4419903\n",
        "files/diffs/1_a.patch": "+MRN 4419904\n",
        "shell/remote/hpc/host-1.rec": "MRN 4419905\n",
        "jobs/412391.out": "MRN 4419906\n",
        "jobs/412391.err": "MRN 4419907\n",
    }
    for relpath, content in files.items():
        target = session.root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    found = {str(path.relative_to(session.root)) for path in iter_text_targets(session.root)}
    assert found == set(files), set(files) ^ found

    store.stop(session.session_id)
    session = store.resolve(session.session_id)
    seal_session(session, force=True, key=b"\x01" * 32)

    for relpath in files:
        text = (session.root / relpath).read_text(encoding="utf-8")
        assert "MRN 44199" not in text, relpath
        assert "MRN_" in text, relpath
