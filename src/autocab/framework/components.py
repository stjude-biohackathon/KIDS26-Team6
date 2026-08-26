"""Default framework components used by the AutoCAB demo pipeline."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from autocab.demo_data import load_skill_catalog
from autocab.input_sources import InputBundle, load_screen_capture_input, load_terminal_log_input, load_trace_input
from autocab.models import (
    MatchResult,
    RedactionReport,
    ReviewDecision,
    SkillProposal,
    SkillReference,
    TraceCluster,
    WorkflowTrace,
)
from pr_generator import export_proposal

STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "the",
    "to",
    "with",
}


class TraceInputAdapter:
    """Adapter for synthetic or public workflow-trace JSON input."""

    mode = "trace"

    def load(self, source_path: Path | None = None) -> InputBundle:
        return load_trace_input(source_path)


class ScreenCaptureInputAdapter:
    """Adapter for pre-exported consented screen-capture timelines."""

    mode = "screen-capture"

    def load(self, source_path: Path | None = None) -> InputBundle:
        return load_screen_capture_input(source_path)


class TerminalLogInputAdapter:
    """Adapter for timestamped terminal workflow logs."""

    mode = "terminal-log"

    def load(self, source_path: Path | None = None) -> InputBundle:
        return load_terminal_log_input(source_path)


class WorkflowClusterer:
    """Default grouping strategy based on workflow family."""

    def cluster(self, traces: list[WorkflowTrace]) -> list[TraceCluster]:
        grouped: dict[str, list[WorkflowTrace]] = defaultdict(list)
        for trace in traces:
            grouped[trace.workflow_family].append(trace)

        clusters: list[TraceCluster] = []
        for index, (workflow_family, members) in enumerate(sorted(grouped.items()), start=1):
            analysts = sorted({trace.analyst for trace in members})
            clusters.append(
                TraceCluster(
                    cluster_id=f"cluster-{index}",
                    workflow_family=workflow_family,
                    analysts=analysts,
                    traces=members,
                )
            )

        return sorted(clusters, key=lambda cluster: (-cluster.frequency, cluster.workflow_family))


class SensitiveDataRedactor:
    """Replace obvious sensitive tokens before proposal generation."""

    def __init__(self, deny_list: tuple[str, ...]) -> None:
        self._deny_list = set(deny_list)
        self._patterns: list[tuple[str, re.Pattern[str]]] = [
            ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
            ("sj_id", re.compile(r"\bSJ[-_ ]?\d{4,8}\b", re.IGNORECASE)),
            ("mrn", re.compile(r"\bMRN[: ]?\d{6,10}\b", re.IGNORECASE)),
            ("dob", re.compile(r"\bDOB[: ]?\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)),
        ]

    def redact(self, cluster: TraceCluster) -> RedactionReport:
        return self.redact_text(cluster.merged_text())

    def redact_text(self, text: str) -> RedactionReport:
        """Redact free-form text outside the full pipeline."""

        redacted = text
        findings: list[str] = []

        for name, pattern in self._patterns:
            if pattern.search(redacted):
                findings.append(name)
                redacted = pattern.sub(f"[REDACTED_{name.upper()}]", redacted)

        lower_text = redacted.lower()
        for token in sorted(self._deny_list):
            if token in lower_text:
                findings.append(f"deny:{token}")
                redacted = re.sub(token, "[REDACTED_TERM]", redacted, flags=re.IGNORECASE)

        return RedactionReport(redacted_text=redacted, findings=sorted(set(findings)))


class SkillCatalog:
    """Loads the focused reference set used for skill matching."""

    def __init__(self, catalog_path: Path | None = None) -> None:
        self._catalog_path = catalog_path

    def load(self) -> list[SkillReference]:
        return load_skill_catalog(self._catalog_path)


class SkillProposalBuilder:
    """Turn redacted workflow clusters into reviewable skill drafts."""

    def __init__(
        self,
        benchmark_dataset: str,
        covered_threshold: float,
        partial_threshold: float,
        composite_threshold: float,
        top_k_matches: int,
    ) -> None:
        self._benchmark_dataset = benchmark_dataset
        self._covered_threshold = covered_threshold
        self._partial_threshold = partial_threshold
        self._composite_threshold = composite_threshold
        self._top_k_matches = top_k_matches

    def build(
        self,
        cluster: TraceCluster,
        skills: list[SkillReference],
        redaction_report: RedactionReport,
    ) -> SkillProposal:
        matches = self._match_skills(redaction_report.redacted_text, skills)
        coverage, proposal_type = self._determine_coverage(matches)
        slug = self._slugify(cluster.workflow_family)
        title = f"{cluster.workflow_family.title()} Skill"
        trace_ids = [trace.trace_id for trace in cluster.traces]
        top_match_names = [match.skill.name for match in matches[:2] if match.score > 0]

        purpose = (
            f"Convert the repeated '{cluster.workflow_family}' workflow into a reusable, "
            "reviewable skill for CAB analysts."
        )
        if top_match_names:
            purpose += f" Reuses ideas from: {', '.join(top_match_names)}."

        inputs = [
            "Public or synthetic workflow trace summary",
            "Paths to public-data analysis outputs",
            "Optional analyst notes for QC or reporting context",
        ]
        outputs = [
            "Structured QC/report summary",
            "Reviewable SKILL.md draft",
            "PR-ready export folder with metadata",
        ]
        steps = [
            "Load and sanitize the workflow trace before any proposal generation.",
            "Check whether the trace is already covered by the current CAB skill catalog.",
            "Assemble reusable steps for QC, figure preparation, and report packaging.",
            "Emit a draft skill proposal with human review and validation notes.",
        ]
        if proposal_type == "composite":
            steps.append("Chain the closest existing skills into one governed workflow recipe.")
        elif proposal_type == "new":
            steps.append("Create a net-new skill because no existing reference covers the workflow.")
        else:
            steps.append("Recommend the existing skill and document any lightweight CAB-specific edits.")

        validation_notes = [
            "Confirm the workflow only uses public or synthetic data.",
            "Verify that redaction removed direct identifiers and sensitive tokens.",
            "Review coverage classification before publishing the proposal.",
            f"Run the workflow on the {self._benchmark_dataset} example or another public benchmark.",
        ]

        return SkillProposal(
            proposal_id=f"proposal-{cluster.cluster_id}",
            slug=slug,
            title=title,
            proposal_type=proposal_type,
            coverage=coverage,
            workflow_family=cluster.workflow_family,
            analysts=cluster.analysts,
            source_trace_ids=trace_ids,
            sanitized_summary=redaction_report.redacted_text,
            matched_skills=matches,
            purpose=purpose,
            inputs=inputs,
            outputs=outputs,
            steps=steps,
            validation_notes=validation_notes,
        )

    def _tokenize(self, text: str) -> set[str]:
        parts = re.findall(r"[a-z0-9]+", text.lower())
        return {part for part in parts if len(part) > 2 and part not in STOP_WORDS}

    def _match_skills(self, text: str, skills: list[SkillReference]) -> list[MatchResult]:
        workflow_terms = self._tokenize(text)
        results: list[MatchResult] = []
        for skill in skills:
            skill_terms = self._tokenize(skill.searchable_text())
            union = workflow_terms | skill_terms
            score = 0.0 if not union else len(workflow_terms & skill_terms) / len(union)
            results.append(
                MatchResult(
                    skill=skill,
                    score=score,
                    shared_terms=sorted(workflow_terms & skill_terms)[:8],
                )
            )

        return sorted(results, key=lambda item: item.score, reverse=True)[: self._top_k_matches]

    def _determine_coverage(self, matches: list[MatchResult]) -> tuple[str, str]:
        top_score = matches[0].score if matches else 0.0
        second_score = matches[1].score if len(matches) > 1 else 0.0
        if top_score >= self._covered_threshold:
            return "covered", "existing"
        if top_score >= self._partial_threshold or (top_score + second_score) >= self._composite_threshold:
            return "partial", "composite"
        return "missing", "new"

    def _slugify(self, text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or "autocab-skill"


class InMemoryReviewService:
    """Minimal human-review queue suitable for the hackathon prototype."""

    def __init__(self) -> None:
        self._proposals: dict[str, SkillProposal] = {}

    def submit(self, proposal: SkillProposal) -> None:
        self._proposals[proposal.proposal_id] = proposal

    def decide(self, proposal_id: str, decision: ReviewDecision) -> SkillProposal:
        proposal = self._proposals[proposal_id]
        proposal.review_status = decision.status
        proposal.reviewer_notes = f"{decision.reviewer}: {decision.notes}".strip()
        return proposal

    def list_pending(self) -> list[SkillProposal]:
        return [
            proposal
            for proposal in self._proposals.values()
            if proposal.review_status == "pending"
        ]


class ProposalExporter:
    """Write approved proposals into a PR-ready skill folder."""

    def export(self, proposal: SkillProposal, output_dir: Path) -> Path:
        return export_proposal(proposal, output_dir)
