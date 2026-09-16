"""The AutoCAB session adapter and multi-analyst merge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocab.framework import build_default_pipeline
from autocab.framework.config import PipelineConfig
from autocab.session_bundle import SessionBundleError, load_session_input
from wfrec.events import Event
from wfrec.merge import merge_sessions


def _populate(session, commands, analyst=None):
    if analyst:
        session.manifest.analyst = analyst
        session.save()
    session.writer.extend(
        [
            Event(
                source="shell",
                type="shell.command.completed",
                payload={"command": c, "cwd": "/data", "exit_code": 0, "duration_ms": 10},
            )
            for c in commands
        ]
    )
    return session


def test_session_mode_is_registered_in_the_pipeline():
    pipeline = build_default_pipeline()
    assert "session" in pipeline._input_adapters
    assert "session" in PipelineConfig().allowed_input_modes


def test_load_a_single_session_folder(store):
    session, _ = store.start(title="HG008 QC", analyst="mgatta42")
    _populate(session, ["fastqc HG008.bam", "multiqc ."])

    bundle = load_session_input(session.root)
    assert bundle.mode == "session"
    assert len(bundle.traces) == 1
    assert bundle.traces[0].analyst == "mgatta42"
    assert "qc" in bundle.traces[0].tags


def test_load_a_directory_of_sessions_for_multi_analyst(store, wfrec_home):
    """Challenge extension (b): several analysts ingested together."""

    first, _ = store.start(title="HG008 QC", analyst="alice")
    _populate(first, ["fastqc HG008.bam"])
    second, _ = store.start(title="HG008 QC", analyst="bob")
    _populate(second, ["fastqc HG008.bam"])

    bundle = load_session_input(wfrec_home / "sessions")
    assert len(bundle.traces) == 2
    assert {t.analyst for t in bundle.traces} == {"alice", "bob"}
    assert "2 analyst(s)" in bundle.source_note


def test_clusterer_groups_two_analysts_into_one_family(store, wfrec_home):
    from autocab.framework.components import WorkflowClusterer

    first, _ = store.start(title="Variant QC", analyst="alice")
    _populate(first, ["bcftools view in.vcf.gz"])
    second, _ = store.start(title="Variant QC", analyst="bob")
    _populate(second, ["bcftools view in.vcf.gz"])

    bundle = load_session_input(wfrec_home / "sessions")
    clusters = WorkflowClusterer().cluster(bundle.traces)

    assert len(clusters) == 1
    assert clusters[0].analysts == ["alice", "bob"]
    assert clusters[0].frequency == 2


def test_load_an_exported_trace_file(store):
    from wfrec.exporters import export_session

    session, _ = store.start(title="A", analyst="a")
    _populate(session, ["ls"])
    result = export_session(session, formats=["trace"])
    path = Path(result["details"]["trace"]["path"])

    bundle = load_session_input(path)
    assert len(bundle.traces) == 1


def test_missing_path_raises_a_helpful_error():
    with pytest.raises(SessionBundleError, match="--session-dir"):
        load_session_input(None)
    with pytest.raises(SessionBundleError, match="No such session path"):
        load_session_input(Path("/nonexistent/wfrec/session"))


def test_non_session_directory_is_rejected_clearly(tmp_path):
    (tmp_path / "random.txt").write_text("hi")
    with pytest.raises(SessionBundleError, match="manifest.json"):
        load_session_input(tmp_path)


def test_empty_session_is_rejected(store):
    session, _ = store.start(title="A", analyst="a")
    # Lifecycle events only -- nothing a skill could be drafted from.
    with pytest.raises(SessionBundleError, match="no convertible workflow steps"):
        load_session_input(session.root)


def test_adapter_rebuilds_rather_than_trusting_a_stale_export(store):
    """A stale exports/trace.json must not shadow newer timeline content."""

    from wfrec.exporters import export_session

    session, _ = store.start(title="A", analyst="a")
    _populate(session, ["first"])
    export_session(session, formats=["trace"])

    _populate(session, ["second"])
    bundle = load_session_input(session.root)
    details = " ".join(step.detail for step in bundle.traces[0].steps)
    assert "second" in details


def test_full_pipeline_run_from_a_session_folder(store, tmp_path):
    """The whole loop: recorded session in, draft SKILL.md out."""

    session, _ = store.start(title="HG008 variant benchmarking", analyst="mgatta42")
    _populate(
        session,
        [
            "samtools stats HG008.bam > stats.txt",
            "bcftools view -i 'QUAL>30' in.vcf.gz > out.vcf",
            "truvari bench -b giab_truth.vcf.gz -c out.vcf -o bench/",
        ],
    )
    session.add_note("Needed the GIAB HG008 truth set", label="why")

    output = tmp_path / "drafts"
    pipeline = build_default_pipeline()
    result = pipeline.run(
        output_dir=output,
        input_mode="session",
        session_path=session.root,
        approve=True,
    )

    assert result.proposals
    proposal = result.proposals[0]
    assert proposal.review_status == "approved"
    assert proposal.analysts == ["mgatta42"]

    skill = output / proposal.slug / "SKILL.md"
    assert skill.exists()
    body = skill.read_text(encoding="utf-8")
    assert body.startswith("---")
    assert "mgatta42" in body


def test_merge_reports_which_families_are_shared(store, tmp_path, wfrec_home):
    first, _ = store.start(title="Variant QC", analyst="alice")
    _populate(first, ["bcftools view in.vcf.gz"])
    second, _ = store.start(title="Variant QC", analyst="bob")
    _populate(second, ["bcftools view in.vcf.gz"])
    third, _ = store.start(title="One-off thing", analyst="carol")
    _populate(third, ["echo unique"])

    out = tmp_path / "merged.json"
    report = merge_sessions(
        [first.session_id, second.session_id, third.session_id], out
    )

    assert report["traces"] == 3
    assert sorted(report["analysts"]) == ["alice", "bob", "carol"]
    assert len(report["multi_analyst_families"]) == 1
    shared = next(iter(report["multi_analyst_families"].values()))
    assert sorted(shared) == ["alice", "bob"]

    traces = json.loads(out.read_text(encoding="utf-8"))
    assert len(traces) == 3


def test_merge_can_force_a_shared_family(store, tmp_path):
    first, _ = store.start(title="QC the HG008 run", analyst="alice")
    _populate(first, ["fastqc a.bam"])
    second, _ = store.start(title="hg008 quality checks", analyst="bob")
    _populate(second, ["fastqc b.bam"])

    out = tmp_path / "merged.json"
    report = merge_sessions(
        [first.session_id, second.session_id], out, workflow_family="hg008 qc"
    )
    assert report["workflow_families"] == {"hg008 qc": ["alice", "bob"]}


def test_merge_skips_empty_sessions(store, tmp_path):
    good, _ = store.start(title="A", analyst="a")
    _populate(good, ["ls"])
    empty, _ = store.start(title="B", analyst="b")

    report = merge_sessions([good.session_id, empty.session_id], tmp_path / "m.json")
    assert report["traces"] == 1
    assert report["skipped"][0]["session"] == empty.session_id
