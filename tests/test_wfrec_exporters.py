"""Exports into AutoCAB's formats, verified by round-tripping.

The important test here is conformance of the terminal-log export: rather than
eyeballing the format, the exporter's own output is fed back through
``autocab.terminal_logs.parse_terminal_session``, which raises ``ValueError`` on
any line it does not recognize.
"""

from __future__ import annotations

import json

import pytest

from autocab.input_sources import load_screen_capture_input
from autocab.terminal_logs import convert_terminal_log, parse_terminal_session
from wfrec.events import Event
from wfrec.exporters import export_session
from wfrec.exporters.autocab_terminal import build_terminal_log, write_terminal_log
from wfrec.exporters.trace import build_trace, infer_tags


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
    parsed = parse_terminal_session(body)   # must not raise
    assert len(parsed.commands) == 1
    assert "\n" not in parsed.commands[0][1]
    assert "print(1)" in parsed.commands[0][1]


def test_timestamps_are_truncated_to_whole_seconds(store):
    """TIMESTAMP_PATTERN accepts no sub-second precision and no timezone."""

    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["ls"])
    body, _ = build_terminal_log(session)

    command_line = [l for l in body.splitlines() if not l.startswith("#")][0]
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
            Event(source="screen", type="screen.ocr", payload={"ocr_text": "samtools flagstat output"}),
            Event(source="screen", type="screen.window", payload={"window_title": "iTerm2 - hg008"}),
        ]
    )
    _record_commands(session, ["samtools flagstat HG008.bam"])

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

    result = export_session(session, formats=["trace"])
    path = __import__("pathlib").Path(result["details"]["trace"]["path"])
    traces = load_workflow_traces(path)

    assert len(traces) == 1
    assert traces[0].analyst == "mgatta42"
    assert "qc" in traces[0].tags


def test_waits_become_workflow_steps(store):
    """A six-hour wait on a job is signal, not a gap to be discarded."""

    session, _ = store.start(title="A", analyst="a")
    session.writer.extend(
        [
            Event(source="session", type="session.paused", payload={"reason": "bwa align", "expect": "6h"}),
            Event(source="session", type="session.waiting", payload={"reason": "bwa align", "elapsed_ms": 3600000}),
            Event(source="session", type="session.resumed", payload={"gap_ms": 21600000}),
        ]
    )
    trace, _steps = build_trace(session)
    waits = [s for s in trace["steps"] if s["action"] == "wait"]
    assert len(waits) == 3
    assert "bwa align" in waits[0]["detail"]


def test_action_hints_map_bioinformatics_tools(store):
    session, _ = store.start(title="A", analyst="a")
    _record_commands(session, ["bwa mem ref.fa r1.fq", "sbatch job.sh", "samtools sort in.bam"])
    trace, _ = build_trace(session)
    actions = {s["detail"].split(":")[1].strip().split()[0]: s["action"] for s in trace["steps"] if s["tool"] == "terminal"}
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
    result = export_session(session, formats=["autocab"])
    names = {__import__("pathlib").Path(p).name for p in result["written"]}
    assert names == {"autocab-terminal.log", "autocab-screen-capture.json"}


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
            Event(source="screen", type="screen.ocr", ts="2026-03-11T09:00:10.000Z",
                  payload={"ocr_text": "later frame"}),
            Event(source="shell", type="shell.command.completed", ts="2026-03-11T09:00:05.000Z",
                  payload={"command": "earlier_command"}),
            Event(source="shell", type="shell.command.completed", ts="2026-03-11T09:00:07.000Z",
                  payload={"command": "middle_command"}),
        ]
    )

    raw = [e.ts for e in session.writer.read()]
    assert raw != sorted(raw), "fixture must actually be out of order"

    body, _ = build_terminal_log(session)
    stamps = [l.split(" ", 1)[0] for l in body.splitlines() if not l.startswith("#")]
    assert stamps == sorted(stamps)

    trace, _ = build_trace(session)
    step_stamps = [s["timestamp"] for s in trace["steps"]]
    assert step_stamps == sorted(step_stamps)
