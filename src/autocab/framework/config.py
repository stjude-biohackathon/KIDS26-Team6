"""Configuration models for the AutoCAB pipeline framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class MatchingPolicy:
    """Thresholds controlling coverage classification."""

    covered_threshold: float = 0.24
    partial_threshold: float = 0.18
    composite_threshold: float = 0.33
    top_k_matches: int = 3


@dataclass(slots=True)
class PipelineConfig:
    """Top-level framework configuration for one AutoCAB run."""

    skill_catalog_path: Path | None = None
    default_output_dir: Path = Path("skills/generated-drafts")
    benchmark_dataset: str = "GIAB HG008"
    allowed_input_modes: tuple[str, ...] = ("trace", "screen-capture", "terminal-log")
    redact_deny_terms: tuple[str, ...] = ("patient", "diagnosis", "pathology")
    matching: MatchingPolicy = field(default_factory=MatchingPolicy)
