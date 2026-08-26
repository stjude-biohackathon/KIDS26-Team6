"""Compatibility wrappers for workflow aggregation and proposal generation."""

from __future__ import annotations

from autocab.framework.components import SkillProposalBuilder, WorkflowClusterer
from autocab.framework.config import MatchingPolicy
from autocab.models import MatchResult, SkillProposal, SkillReference, TraceCluster, WorkflowTrace


def aggregate_traces(traces: list[WorkflowTrace]) -> list[TraceCluster]:
    """Group repeated workflows across analysts."""

    return WorkflowClusterer().cluster(traces)


def match_skills(text: str, skills: list[SkillReference], limit: int = 3) -> list[MatchResult]:
    """Match a workflow summary against the reference skill catalog."""

    policy = MatchingPolicy(top_k_matches=limit)
    builder = SkillProposalBuilder(
        benchmark_dataset="GIAB HG008",
        covered_threshold=policy.covered_threshold,
        partial_threshold=policy.partial_threshold,
        composite_threshold=policy.composite_threshold,
        top_k_matches=policy.top_k_matches,
    )
    return builder._match_skills(text, skills)


def _determine_coverage(matches: list[MatchResult]) -> tuple[str, str]:
    policy = MatchingPolicy()
    builder = SkillProposalBuilder(
        benchmark_dataset="GIAB HG008",
        covered_threshold=policy.covered_threshold,
        partial_threshold=policy.partial_threshold,
        composite_threshold=policy.composite_threshold,
        top_k_matches=policy.top_k_matches,
    )
    return builder._determine_coverage(matches)


def _slugify(text: str) -> str:
    builder = SkillProposalBuilder(
        benchmark_dataset="GIAB HG008",
        covered_threshold=0.24,
        partial_threshold=0.18,
        composite_threshold=0.33,
        top_k_matches=3,
    )
    return builder._slugify(text)


def propose_skill(
    cluster: TraceCluster,
    skills: list[SkillReference],
    sanitized_summary: str,
) -> SkillProposal:
    """Create a draft skill proposal from an aggregated workflow cluster."""

    policy = MatchingPolicy()
    builder = SkillProposalBuilder(
        benchmark_dataset="GIAB HG008",
        covered_threshold=policy.covered_threshold,
        partial_threshold=policy.partial_threshold,
        composite_threshold=policy.composite_threshold,
        top_k_matches=policy.top_k_matches,
    )
    from autocab.models import RedactionReport

    return builder.build(
        cluster,
        skills,
        RedactionReport(redacted_text=sanitized_summary, findings=[]),
    )
