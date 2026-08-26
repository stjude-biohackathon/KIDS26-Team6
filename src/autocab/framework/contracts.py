"""Component contracts for the AutoCAB pipeline framework."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from autocab.input_sources import InputBundle
from autocab.models import RedactionReport, ReviewDecision, SkillProposal, SkillReference, TraceCluster, WorkflowTrace


class InputAdapter(Protocol):
    """Loads workflow observations into normalized workflow traces."""

    mode: str

    def load(self, source_path: Path | None = None) -> InputBundle:
        """Load input data for one pipeline run."""


class Clusterer(Protocol):
    """Groups repeated workflow traces."""

    def cluster(self, traces: list[WorkflowTrace]) -> list[TraceCluster]:
        """Group related workflow traces into reviewable clusters."""


class Redactor(Protocol):
    """Applies privacy controls before proposal generation."""

    def redact(self, cluster: TraceCluster) -> RedactionReport:
        """Return a redaction report for the candidate cluster."""


class ProposalBuilder(Protocol):
    """Builds a skill proposal from a redacted workflow cluster."""

    def build(
        self,
        cluster: TraceCluster,
        skills: list[SkillReference],
        redaction_report: RedactionReport,
    ) -> SkillProposal:
        """Create a draft skill proposal."""


class ReviewService(Protocol):
    """Stores proposals and applies human review decisions."""

    def submit(self, proposal: SkillProposal) -> None:
        """Queue a proposal for review."""

    def decide(self, proposal_id: str, decision: ReviewDecision) -> SkillProposal:
        """Apply a review decision to a queued proposal."""


class Exporter(Protocol):
    """Writes approved proposals to a PR-ready folder."""

    def export(self, proposal: SkillProposal, output_dir: Path) -> Path:
        """Persist the proposal into an export directory."""
