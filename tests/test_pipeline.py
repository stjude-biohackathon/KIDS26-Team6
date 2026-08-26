from pathlib import Path

from aggregation import aggregate_traces
from autocab.demo_data import load_skill_catalog, load_workflow_traces
from autocab.input_sources import load_screen_capture_input, load_terminal_log_input, load_trace_input
from autocab.orchestrator import run_demo, run_pipeline


def test_trace_aggregation_groups_repeated_workflows():
    traces = load_workflow_traces()

    clusters = aggregate_traces(traces)

    assert clusters[0].workflow_family == "hg008 qc report assembly"
    assert clusters[0].frequency == 2
    assert clusters[0].analysts == ["analyst-a", "analyst-b"]


def test_demo_pipeline_exports_approved_proposals(tmp_path: Path):
    proposals = run_demo(tmp_path, reviewer="Test Reviewer", approve=True)

    assert len(proposals) == 2
    first = proposals[0]
    assert first.review_status == "approved"
    assert first.export_path is not None
    assert (first.export_path / "SKILL.md").exists()
    assert (first.export_path / "metadata.json").exists()
    assert "[REDACTED_EMAIL]" in first.sanitized_summary


def test_skill_catalog_and_workflow_samples_are_loadable():
    skills = load_skill_catalog()
    traces = load_workflow_traces()

    assert len(skills) == 4
    assert len(traces) == 3
    assert any("GIAB" in trace.summary for trace in traces)


def test_trace_input_mode_uses_json_source():
    bundle = load_trace_input()

    assert bundle.mode == "trace"
    assert len(bundle.traces) == 3
    assert "sample_workflow_traces.json" in bundle.source_note


def test_screen_capture_input_maps_sessions_to_traces():
    bundle = load_screen_capture_input()

    assert bundle.mode == "screen-capture"
    assert len(bundle.traces) == 3
    assert bundle.traces[0].source == "screen-capture"
    assert bundle.traces[0].steps[0].tool == "terminal"
    assert "sample_screen_capture.json" in bundle.source_note


def test_screen_capture_pipeline_exports_approved_proposals(tmp_path: Path):
    proposals = run_pipeline(
        output_dir=tmp_path,
        input_mode="screen-capture",
        approve=True,
    )

    assert len(proposals) == 2
    assert proposals[0].review_status == "approved"
    assert "[REDACTED_EMAIL]" in proposals[0].sanitized_summary
    assert "[REDACTED_MRN]" in proposals[0].sanitized_summary


def test_terminal_log_input_maps_log_to_trace():
    bundle = load_terminal_log_input()

    assert bundle.mode == "terminal-log"
    assert len(bundle.traces) == 1
    assert bundle.traces[0].steps[0].tool == "terminal"
    assert "sample_terminal_session.log" in bundle.source_note


def test_terminal_log_pipeline_exports_approved_proposal(tmp_path: Path):
    proposals = run_pipeline(
        output_dir=tmp_path,
        input_mode="terminal-log",
        approve=True,
    )

    assert len(proposals) == 1
    assert proposals[0].review_status == "approved"
    assert proposals[0].export_path is not None
