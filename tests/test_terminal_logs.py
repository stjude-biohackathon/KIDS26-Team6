from pathlib import Path

from autocab.input_sources import load_terminal_log_input
from autocab.terminal_logs import convert_terminal_log, parse_terminal_session, terminal_session_to_trace


def test_parse_terminal_session_reads_metadata_and_commands():
    text = "\n".join(
        [
            "# analyst: analyst-a",
            "# workflow_family: hg008 qc report assembly",
            "2026-07-21T09:00:00 ls results/hg008",
            "2026-07-21T09:05:00 python3 scripts/qc_summary.py --sample HG008",
        ]
    )

    session = parse_terminal_session(text)

    assert session.metadata["analyst"] == "analyst-a"
    assert len(session.commands) == 2
    assert session.commands[1][1].startswith("python3")


def test_terminal_session_to_trace_infers_tags_and_steps():
    text = "\n".join(
        [
            "# analyst: analyst-a",
            "# title: HG008 QC and report assembly",
            "2026-07-21T09:00:00 ls results/hg008",
            "2026-07-21T09:05:00 python3 scripts/qc_summary.py --sample HG008 --input results/hg008/qc.tsv",
            "2026-07-21T09:10:00 python3 scripts/build_report.py --output reports/hg008_report.md",
        ]
    )

    trace = terminal_session_to_trace(parse_terminal_session(text))

    assert trace.analyst == "analyst-a"
    assert trace.steps[0].tool == "terminal"
    assert trace.steps[0].action == "inspect"
    assert "hg008" in trace.tags
    assert "qc" in trace.tags
    assert "report" in trace.tags


def test_convert_terminal_log_and_input_loader_use_sample_file():
    trace = convert_terminal_log(
        Path("data/sample_terminal_session.log")
    )
    bundle = load_terminal_log_input()

    assert trace.workflow_family == "hg008 qc report assembly"
    assert len(trace.steps) == 4
    assert bundle.mode == "terminal-log"
    assert bundle.traces[0].source.startswith("terminal-log:")
