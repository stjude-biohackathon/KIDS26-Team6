"""Session exports, including AutoCAB adapters verified by round-tripping.

The important test here is conformance of the terminal-log export: rather than
eyeballing the format, the exporter's own output is fed back through
``autocab.terminal_logs.parse_terminal_session``, which raises ``ValueError`` on
any line it does not recognize.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocab.input_sources import load_screen_capture_input
from autocab.terminal_logs import convert_terminal_log, parse_terminal_session
from autocab.recording.events import Event
from autocab.recording.exporters import export_session
from autocab.recording.exporters.autocab_terminal import build_terminal_log, write_terminal_log
from autocab.recording.exporters.snapshot import ExportStateError, export_session_safely
from autocab.recording.exporters.trace import build_trace, infer_tags


def _seal(session):
    """``export_session`` refuses an unsealed session -- that refusal is the
    control, so the export tests seal rather than have the gate relaxed."""

    from autocab.recording.seal import seal_session

    return seal_session(session, key=b"\x2a" * 32, engine_label="regex", force=True)


def _record_commands(session, commands):
    session.writer.extend(
        [
            Event(
                source="shell",
                type="shell.command.completed",
                payload={
                    "command": command,
                    "cwd": "/data/hg008",
                    "exit_code": 0,
                    "duration_ms": 120,
                    "shell": "bash",
                },
            )
            for command in commands
        ]
    )


def test_terminal_log_round_trips_through_the_strict_parser(store):
    session, _ = store.start(title="HG008 QC", analyst="mgatta42")
    _record_commands(
        session,
        [
            "samtools stats HG008.bam > stats.txt",
            "bcftools view -i 'QUAL>30' in.vcf.gz > out.vcf",
            "truvari bench -b giab_truth.vcf.gz -c out.vcf -o bench/",
        ],
    )

    body, count = build_terminal_log(session)
    parsed = parse_terminal_session(body)

    assert count == 3
    assert len(parsed.commands) == 3
    assert parsed.metadata["analyst"] == "mgatta42"
    assert parsed.metadata["title"] == "HG008 QC"


def test_multiline_commands_are_flattened_not_dropped(store):
    """The parser cannot represent a newline, so it must become a marker.

    Heredocs and pasted scripts are common; silently dropping them would lose
    real workflow steps, and emitting them raw would make the whole import fail.
    """

    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["python <<EOF\nprint(1)\nEOF"])

    body, _count = build_terminal_log(session)
    parsed = parse_terminal_session(body)  # must not raise
    assert len(parsed.commands) == 1
    assert "\n" not in parsed.commands[0][1]
    assert "print(1)" in parsed.commands[0][1]


def test_timestamps_are_truncated_to_whole_seconds(store):
    """TIMESTAMP_PATTERN accepts no sub-second precision and no timezone."""

    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["ls"])
    body, _ = build_terminal_log(session)

    command_line = [line for line in body.splitlines() if not line.startswith("#")][0]
    stamp = command_line.split(" ", 1)[0]
    assert "." not in stamp and "Z" not in stamp
    assert len(stamp) == 19


def test_empty_commands_are_skipped(store):
    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["ls", "   ", ""])
    body, count = build_terminal_log(session)
    assert count == 1
    parse_terminal_session(body)


def test_written_log_loads_through_convert_terminal_log(store):
    session, _ = store.start(title="Variant benchmark", analyst="mgatta42")
    _record_commands(session, ["truvari bench -b giab.vcf.gz -c hg008.vcf"])

    path, _count = write_terminal_log(session)
    trace = convert_terminal_log(path)

    assert trace.analyst == "mgatta42"
    assert trace.steps and trace.steps[0].tool == "terminal"
    assert "benchmark" in trace.tags


def test_screen_capture_export_loads_through_existing_adapter(store):
    """OCR text and window titles must land in the fields the adapter reads."""

    session, _ = store.start(title="A", analyst="mgatta42")
    session.writer.extend(
        [
            Event(
                source="screen", type="screen.ocr", payload={"ocr_text": "samtools flagstat output"}
            ),
            Event(
                source="screen", type="screen.window", payload={"window_title": "iTerm2 - hg008"}
            ),
        ]
    )
    _record_commands(session, ["samtools flagstat HG008.bam"])

    _seal(session)
    result = export_session(session, formats=["screen-capture"])
    path = result["details"]["screen_capture"]["path"]

    bundle = load_screen_capture_input(__import__("pathlib").Path(path))
    assert len(bundle.traces) == 1
    details = " ".join(step.detail for step in bundle.traces[0].steps)
    assert "samtools flagstat output" in details

    document = json.loads(open(path, encoding="utf-8").read())
    events = document["sessions"][0]["events"]
    assert any(e["ocr_text"] for e in events)
    assert any(e["window_title"] for e in events)


def test_trace_export_is_a_list_matching_load_workflow_traces(store):
    from autocab.demo_data import load_workflow_traces

    session, _ = store.start(title="A", analyst="mgatta42")
    _record_commands(session, ["fastqc HG008.bam", "multiqc ."])

    _seal(session)
    result = export_session(session, formats=["trace"])
    path = __import__("pathlib").Path(result["details"]["trace"]["path"])
    traces = load_workflow_traces(path)

    assert len(traces) == 1
    assert traces[0].analyst == "mgatta42"
    assert "qc" in traces[0].tags


def test_events_json_preserves_the_complete_sealed_session(store):
    session, _ = store.start(title="Variant QC", analyst="analyst-1")
    _record_commands(session, ["fastqc sample.bam"])
    seal = _seal(session)

    result = export_session(session, formats=["events"])

    path = Path(result["details"]["events"]["path"])
    document = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "events.json"
    assert document["schema_version"] == 1
    assert document["session"]["session_id"] == session.session_id
    assert document["session"]["title"] == "Variant QC"
    assert document["seal"]["generation"] == seal.record["generation"]
    assert document["events"] == [event.to_dict() for event in session.writer.read()]
    assert result["details"]["events"]["events"] == len(document["events"])


def test_waits_become_workflow_steps(store):
    """A six-hour wait on a job is signal, not a gap to be discarded."""

    session, _ = store.start(title="A", analyst="a")
    session.writer.extend(
        [
            Event(
                source="session",
                type="session.paused",
                payload={"reason": "bwa align", "expect": "6h"},
            ),
            Event(
                source="session",
                type="session.waiting",
                payload={"reason": "bwa align", "elapsed_ms": 3600000},
            ),
            Event(source="session", type="session.resumed", payload={"gap_ms": 21600000}),
        ]
    )
    trace, _steps = build_trace(session)
    waits = [s for s in trace["steps"] if s["action"] == "wait"]
    assert len(waits) == 3
    assert "bwa align" in waits[0]["detail"]


def test_trace_projects_each_supported_event_without_changing_details(store):
    session, _ = store.start(title="A", analyst="a")
    session.writer.extend(
        [
            Event(
                source="shell",
                type="shell.command.completed",
                ts="2030-01-01T00:00:01.000Z",
                host="node1",
                origin="remote",
                payload={
                    "command": "bwa mem ref.fa reads.fq",
                    "cwd": "/data",
                    "exit_code": 0,
                    "duration_ms": 15,
                },
            ),
            Event(
                source="screen",
                type="screen.ocr",
                ts="2030-01-01T00:00:02.000Z",
                payload={"ocr_text": "screen text"},
            ),
            Event(
                source="notes",
                type="context.note",
                ts="2030-01-01T00:00:03.000Z",
                payload={"label": "decision", "text": "keep sample"},
            ),
            Event(
                source="agents",
                type="agent.message",
                ts="2030-01-01T00:00:04.000Z",
                payload={
                    "tool": "codex",
                    "role": "assistant",
                    "text": "inspect output",
                },
            ),
            Event(
                source="files",
                type="file.diff",
                ts="2030-01-01T00:00:05.000Z",
                payload={"path": "workflow.py", "added": 3, "deleted": 1},
            ),
            Event(
                source="files",
                type="git.snapshot",
                ts="2030-01-01T00:00:06.000Z",
                payload={"trigger": "manual", "branch": "main", "changed_count": 2},
            ),
            Event(
                source="jobs",
                type="job.submitted",
                ts="2030-01-01T00:00:07.000Z",
                payload={
                    "scheduler": "slurm",
                    "job_id": "42",
                    "jobname": "align",
                    "workdir": "/work",
                },
            ),
            Event(
                source="session",
                type="session.paused",
                ts="2030-01-01T00:00:08.000Z",
                payload={"reason": "wait", "expect": "6h"},
            ),
            Event(
                source="session",
                type="session.resumed",
                ts="2030-01-01T00:00:09.000Z",
                payload={"gap_ms": 5000},
            ),
            Event(
                source="session",
                type="session.waiting",
                ts="2030-01-01T00:00:10.000Z",
                payload={"reason": "wait", "elapsed_ms": 1000},
            ),
            Event(
                source="notes",
                type="marker.user",
                ts="2030-01-01T00:00:11.000Z",
                payload={"label": "review", "detail": "accepted"},
            ),
            Event(
                source="deid",
                type="deid.sealed",
                ts="2030-01-01T00:00:12.000Z",
            ),
            Event(
                source="screen",
                type="screen.window",
                ts="2030-01-01T00:00:13.000Z",
            ),
        ]
    )

    trace, step_count = build_trace(session)

    assert step_count == 11
    assert [
        {key: step[key] for key in ("tool", "action", "detail")} for step in trace["steps"]
    ] == [
        {
            "tool": "terminal",
            "action": "align",
            "detail": (
                "Executed command: bwa mem ref.fa reads.fq cwd=/data exit=0 "
                "duration=15ms host=node1"
            ),
        },
        {"tool": "screen", "action": "observe", "detail": "Screen text: screen text"},
        {
            "tool": "notes",
            "action": "annotate",
            "detail": "Analyst note (decision): keep sample",
        },
        {"tool": "codex", "action": "converse", "detail": "assistant: inspect output"},
        {"tool": "editor", "action": "edit", "detail": "Edited workflow.py (+3/-1)"},
        {
            "tool": "git",
            "action": "version",
            "detail": "Git snapshot (manual) on branch main: 2 changed paths",
        },
        {
            "tool": "scheduler",
            "action": "submit",
            "detail": "Submitted slurm job 42 align workdir=/work",
        },
        {"tool": "session", "action": "wait", "detail": "Paused: wait (expected 6h)"},
        {"tool": "session", "action": "wait", "detail": "Resumed after 5000ms"},
        {
            "tool": "session",
            "action": "wait",
            "detail": "Still waiting (wait) after 1000ms",
        },
        {"tool": "notes", "action": "mark", "detail": "Marker review: accepted"},
    ]


def test_action_hints_map_bioinformatics_tools(store):
    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["bwa mem ref.fa r1.fq", "sbatch job.sh", "samtools sort in.bam"])
    trace, _ = build_trace(session)
    actions = {
        s["detail"].split(":")[1].strip().split()[0]: s["action"]
        for s in trace["steps"]
        if s["tool"] == "terminal"
    }
    assert actions["bwa"] == "align"
    assert actions["sbatch"] == "submit"
    assert actions["samtools"] == "analyze"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("truvari bench against giab HG008", "benchmark"),
        ("fastqc and multiqc report", "qc"),
        ("sbatch align.sh on slurm", "hpc"),
        ("snakemake --cores 8", "workflow"),
    ],
)
def test_tag_inference(text, expected):
    assert expected in infer_tags(text, [])


def test_export_autocab_writes_both_text_formats(store):
    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["ls"])
    _seal(session)
    result = export_session(session, formats=["autocab"])
    names = {__import__("pathlib").Path(p).name for p in result["written"]}
    assert names == {"terminal.log", "screen-events.json"}


def test_export_all_writes_the_four_user_facing_files(store):
    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["ls"])
    _seal(session)

    result = export_session(session, formats=["all"])

    names = {Path(path).name for path in result["written"]}
    assert names == {
        "events.json",
        "workflow-trace.json",
        "terminal.log",
        "screen-events.json",
    }


def test_paused_export_uses_a_redacted_snapshot_and_remains_resumable(
    store,
    tmp_path: Path,
):
    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["echo MRN 4419902"])
    paused = store.pause(session.session_id)
    manifest_before = (paused.root / "manifest.json").read_bytes()
    events_before = (paused.root / "events.jsonl").read_bytes()

    result = export_session_safely(paused, formats=["events"], destination=tmp_path)

    exported_path = tmp_path / f"{paused.session_id}-events.json"
    exported = exported_path.read_text(encoding="utf-8")
    assert "4419902" not in exported
    assert result["privacy"] == {
        "checked": True,
        "method": "protected-snapshot",
        "source_status": "paused",
    }
    assert not (paused.root / "seal.json").exists()
    assert (paused.root / "manifest.json").read_bytes() == manifest_before
    assert (paused.root / "events.jsonl").read_bytes() == events_before

    resumed, _ = store.resume(paused.session_id)
    assert resumed.manifest.status == "active"


def test_archived_unsealed_session_exports_through_a_snapshot(store, tmp_path: Path):
    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["ls"])
    archived = store.stop(session.session_id)

    result = export_session_safely(archived, formats=["all"], destination=tmp_path)

    assert result["privacy"]["method"] == "protected-snapshot"
    assert result["privacy"]["source_status"] == "stopped"
    assert not (archived.root / "seal.json").exists()
    assert {Path(path).name for path in result["written"]} == {
        f"{archived.session_id}-events.json",
        f"{archived.session_id}-workflow-trace.json",
        f"{archived.session_id}-terminal.log",
        f"{archived.session_id}-screen-events.json",
    }


def test_active_unsealed_session_cannot_be_exported(store, tmp_path: Path):
    session, _ = store.start(title="A", analyst="a")

    with pytest.raises(ExportStateError, match="Pause or archive"):
        export_session_safely(session, destination=tmp_path)


def test_exports_are_chronological_even_when_the_file_is_not(store):
    """The timeline is append-ordered by seq, which is not ts order.

    A shell hook stamps a command the instant it finishes, but the daemon
    ingests it up to a second later, so a shell event can carry an earlier ts
    than a screen frame with a lower seq. Anything producing an ordered
    narrative has to sort.
    """

    session, _ = store.start(title="A", analyst="a")
    # Written out of order on purpose, exactly as concurrent collectors would.
    session.writer.extend(
        [
            Event(
                source="screen",
                type="screen.ocr",
                ts="2026-03-11T09:00:10.000Z",
                payload={"ocr_text": "later frame"},
            ),
            Event(
                source="shell",
                type="shell.command.completed",
                ts="2026-03-11T09:00:05.000Z",
                payload={"command": "earlier_command"},
            ),
            Event(
                source="shell",
                type="shell.command.completed",
                ts="2026-03-11T09:00:07.000Z",
                payload={"command": "middle_command"},
            ),
        ]
    )

    raw = [e.ts for e in session.writer.read()]
    assert raw != sorted(raw), "fixture must actually be out of order"

    body, _ = build_terminal_log(session)
    stamps = [line.split(" ", 1)[0] for line in body.splitlines() if not line.startswith("#")]
    assert stamps == sorted(stamps)

    trace, _ = build_trace(session)
    step_stamps = [s["timestamp"] for s in trace["steps"]]
    assert step_stamps == sorted(step_stamps)
