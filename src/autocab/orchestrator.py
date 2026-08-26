"""End-to-end orchestration for the AutoCAB demo pipeline."""

from __future__ import annotations

from pathlib import Path

from .framework import build_default_pipeline
from .models import SkillProposal


def run_pipeline(
    output_dir: Path,
    input_mode: str = "trace",
    trace_path: Path | None = None,
    capture_path: Path | None = None,
    log_path: Path | None = None,
    reviewer: str = "CAB Maintainer",
    approve: bool = True,
) -> list[SkillProposal]:
    """Run the bundled BioHackathon pipeline."""

    pipeline = build_default_pipeline()
    result = pipeline.run(
        output_dir=output_dir,
        input_mode=input_mode,
        trace_path=trace_path,
        capture_path=capture_path,
        log_path=log_path,
        reviewer=reviewer,
        approve=approve,
    )
    return result.proposals


def run_demo(
    output_dir: Path,
    reviewer: str = "CAB Maintainer",
    approve: bool = True,
) -> list[SkillProposal]:
    """Run the safe trace-based BioHackathon demo."""

    return run_pipeline(
        output_dir=output_dir,
        input_mode="trace",
        reviewer=reviewer,
        approve=approve,
    )
