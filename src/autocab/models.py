"""Core data structures for the AutoCAB proof of concept."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class WorkflowStep:
    """One timestamped action taken during a workflow."""

    timestamp: str
    tool: str
    action: str
    detail: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowStep":
        return cls(
            timestamp=payload["timestamp"],
            tool=payload["tool"],
            action=payload["action"],
            detail=payload["detail"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "tool": self.tool,
            "action": self.action,
            "detail": self.detail,
        }


@dataclass(slots=True)
class WorkflowTrace:
    """Observed workflow data from a single analyst session."""

    trace_id: str
    analyst: str
    title: str
    summary: str
    tags: list[str]
    source: str
    workflow_family: str
    steps: list[WorkflowStep] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowTrace":
        return cls(
            trace_id=payload["trace_id"],
            analyst=payload["analyst"],
            title=payload["title"],
            summary=payload["summary"],
            tags=list(payload.get("tags", [])),
            source=payload.get("source", "synthetic"),
            workflow_family=payload.get("workflow_family", payload["title"]),
            steps=[WorkflowStep.from_dict(item) for item in payload.get("steps", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "analyst": self.analyst,
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "source": self.source,
            "workflow_family": self.workflow_family,
            "steps": [step.to_dict() for step in self.steps],
        }

    def searchable_text(self) -> str:
        detail_text = " ".join(step.detail for step in self.steps)
        tag_text = " ".join(self.tags)
        return f"{self.title} {self.summary} {tag_text} {detail_text}"


@dataclass(slots=True)
class SkillReference:
    """Existing skill that can be matched against workflow traces."""

    skill_id: str
    name: str
    summary: str
    tags: list[str]
    maturity: str
    source_repo: str
    path_hint: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SkillReference":
        return cls(
            skill_id=payload["skill_id"],
            name=payload["name"],
            summary=payload["summary"],
            tags=list(payload.get("tags", [])),
            maturity=payload.get("maturity", "prototype"),
            source_repo=payload.get("source_repo", "public"),
            path_hint=payload.get("path_hint", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "summary": self.summary,
            "tags": self.tags,
            "maturity": self.maturity,
            "source_repo": self.source_repo,
            "path_hint": self.path_hint,
        }

    def searchable_text(self) -> str:
        return f"{self.name} {self.summary} {' '.join(self.tags)}"


@dataclass(slots=True)
class MatchResult:
    """Similarity result for a candidate skill."""

    skill: SkillReference
    score: float
    shared_terms: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill.skill_id,
            "name": self.skill.name,
            "score": round(self.score, 3),
            "shared_terms": self.shared_terms,
        }


@dataclass(slots=True)
class RedactionReport:
    """Summary of redactions applied to text."""

    redacted_text: str
    findings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "redacted_text": self.redacted_text,
            "findings": self.findings,
        }


@dataclass(slots=True)
class TraceCluster:
    """Grouped workflow traces from one or more analysts."""

    cluster_id: str
    workflow_family: str
    analysts: list[str]
    traces: list[WorkflowTrace]

    @property
    def frequency(self) -> int:
        return len(self.traces)

    def merged_text(self) -> str:
        return " ".join(trace.searchable_text() for trace in self.traces)


@dataclass(slots=True)
class SkillProposal:
    """Draft skill proposal created from an observed workflow."""

    proposal_id: str
    slug: str
    title: str
    proposal_type: str
    coverage: str
    workflow_family: str
    analysts: list[str]
    source_trace_ids: list[str]
    sanitized_summary: str
    matched_skills: list[MatchResult]
    purpose: str
    inputs: list[str]
    outputs: list[str]
    steps: list[str]
    validation_notes: list[str]
    review_status: str = "pending"
    reviewer_notes: str = ""
    export_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "slug": self.slug,
            "title": self.title,
            "proposal_type": self.proposal_type,
            "coverage": self.coverage,
            "workflow_family": self.workflow_family,
            "analysts": self.analysts,
            "source_trace_ids": self.source_trace_ids,
            "sanitized_summary": self.sanitized_summary,
            "matched_skills": [match.to_dict() for match in self.matched_skills],
            "purpose": self.purpose,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "steps": self.steps,
            "validation_notes": self.validation_notes,
            "review_status": self.review_status,
            "reviewer_notes": self.reviewer_notes,
            "export_path": str(self.export_path) if self.export_path else None,
        }


@dataclass(slots=True)
class ReviewDecision:
    """Human review decision for a proposal."""

    reviewer: str
    status: str
    notes: str = ""
