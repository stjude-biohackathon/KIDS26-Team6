"""Extensible pipeline framework for the AutoCAB BioHackathon prototype."""

from .bootstrap import build_default_pipeline
from .config import PipelineConfig
from .pipeline import AutoCABPipeline, PipelineContext, PipelineResult

__all__ = [
    "AutoCABPipeline",
    "PipelineConfig",
    "PipelineContext",
    "PipelineResult",
    "build_default_pipeline",
]
