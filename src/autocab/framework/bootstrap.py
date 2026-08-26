"""Factory helpers for creating the default AutoCAB pipeline."""

from __future__ import annotations

from .components import (
    InMemoryReviewService,
    ProposalExporter,
    ScreenCaptureInputAdapter,
    SensitiveDataRedactor,
    SkillCatalog,
    SkillProposalBuilder,
    TerminalLogInputAdapter,
    TraceInputAdapter,
    WorkflowClusterer,
)
from .config import PipelineConfig
from .pipeline import AutoCABPipeline


def build_default_pipeline(config: PipelineConfig | None = None) -> AutoCABPipeline:
    """Assemble the default BioHackathon framework components."""

    resolved = config or PipelineConfig()
    return AutoCABPipeline(
        config=resolved,
        skill_catalog_loader=SkillCatalog(resolved.skill_catalog_path),
        input_adapters={
            "trace": TraceInputAdapter(),
            "screen-capture": ScreenCaptureInputAdapter(),
            "terminal-log": TerminalLogInputAdapter(),
        },
        clusterer=WorkflowClusterer(),
        redactor=SensitiveDataRedactor(resolved.redact_deny_terms),
        proposal_builder=SkillProposalBuilder(
            benchmark_dataset=resolved.benchmark_dataset,
            covered_threshold=resolved.matching.covered_threshold,
            partial_threshold=resolved.matching.partial_threshold,
            composite_threshold=resolved.matching.composite_threshold,
            top_k_matches=resolved.matching.top_k_matches,
        ),
        review_service=InMemoryReviewService(),
        exporter=ProposalExporter(),
    )
