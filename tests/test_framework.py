from pathlib import Path

from autocab.framework import PipelineConfig, build_default_pipeline


def test_default_pipeline_returns_context_and_proposals(tmp_path: Path):
    pipeline = build_default_pipeline(PipelineConfig(default_output_dir=tmp_path))

    result = pipeline.run(output_dir=tmp_path, input_mode="trace", approve=False)

    assert result.context.input_mode == "trace"
    assert result.context.input_bundle is not None
    assert len(result.context.skills) == 4
    assert len(result.context.clusters) == 2
    assert len(result.proposals) == 2
    assert result.proposals[0].review_status == "pending"


def test_default_pipeline_exports_when_approved(tmp_path: Path):
    pipeline = build_default_pipeline(PipelineConfig(default_output_dir=tmp_path))

    result = pipeline.run(output_dir=tmp_path, input_mode="screen-capture", approve=True)

    assert len(result.proposals) == 2
    assert result.proposals[0].export_path is not None
    assert (result.proposals[0].export_path / "SKILL.md").exists()


def test_default_pipeline_supports_terminal_logs(tmp_path: Path):
    pipeline = build_default_pipeline(PipelineConfig(default_output_dir=tmp_path))

    result = pipeline.run(output_dir=tmp_path, input_mode="terminal-log", approve=False)

    assert len(result.proposals) == 1
    assert result.context.input_bundle is not None
    assert result.context.input_bundle.mode == "terminal-log"
