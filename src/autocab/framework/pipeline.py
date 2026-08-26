"""Composable pipeline orchestration for AutoCAB."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autocab.input_sources import InputBundle
from autocab.models import RedactionReport, ReviewDecision, SkillProposal, SkillReference, TraceCluster

from .config import PipelineConfig
from .contracts import Clusterer, Exporter, InputAdapter, ProposalBuilder, Redactor, ReviewService


@dataclass(slots=True)
class PipelineContext:
    """Execution context captured for one end-to-end AutoCAB run."""

    input_mode: str
    reviewer: str
    approve: bool
    output_dir: Path
    input_bundle: InputBundle | None = None
    skills: list[SkillReference] = field(default_factory=list)
    clusters: list[TraceCluster] = field(default_factory=list)
    redaction_reports: dict[str, RedactionReport] = field(default_factory=dict)


@dataclass(slots=True)
class PipelineResult:
    """Structured result for one framework run."""

    context: PipelineContext
    proposals: list[SkillProposal]


class AutoCABPipeline:
    """Extensible AutoCAB pipeline for the BioHackathon prototype."""

    def __init__(
        self,
        *,
        config: PipelineConfig,
        skill_catalog_loader,
        input_adapters: dict[str, InputAdapter],
        clusterer: Clusterer,
        redactor: Redactor,
        proposal_builder: ProposalBuilder,
        review_service: ReviewService,
        exporter: Exporter,
    ) -> None:
        self._config = config
        self._skill_catalog_loader = skill_catalog_loader
        self._input_adapters = input_adapters
        self._clusterer = clusterer
        self._redactor = redactor
        self._proposal_builder = proposal_builder
        self._review_service = review_service
        self._exporter = exporter

    def run(
        self,
        *,
        output_dir: Path | None = None,
        input_mode: str = "trace",
        trace_path: Path | None = None,
        capture_path: Path | None = None,
        log_path: Path | None = None,
        reviewer: str = "CAB Maintainer",
        approve: bool = True,
    ) -> PipelineResult:
        if input_mode not in self._config.allowed_input_modes:
            raise ValueError(f"Unsupported input mode: {input_mode}")

        if input_mode == "trace":
            source_path = trace_path
        elif input_mode == "screen-capture":
            source_path = capture_path
        else:
            source_path = log_path
        adapter = self._input_adapters[input_mode]
        context = PipelineContext(
            input_mode=input_mode,
            reviewer=reviewer,
            approve=approve,
            output_dir=output_dir or self._config.default_output_dir,
        )

        context.skills = self._skill_catalog_loader.load()
        context.input_bundle = adapter.load(source_path)
        context.clusters = self._clusterer.cluster(context.input_bundle.traces)

        proposals: list[SkillProposal] = []
        for cluster in context.clusters:
            report = self._redactor.redact(cluster)
            context.redaction_reports[cluster.cluster_id] = report
            proposal = self._proposal_builder.build(cluster, context.skills, report)
            self._review_service.submit(proposal)
            if approve:
                self._review_service.decide(
                    proposal.proposal_id,
                    ReviewDecision(
                        reviewer=reviewer,
                        status="approved",
                        notes="Approved for BioHackathon demo export.",
                    ),
                )
                self._exporter.export(proposal, context.output_dir)
            proposals.append(proposal)

        return PipelineResult(context=context, proposals=proposals)
