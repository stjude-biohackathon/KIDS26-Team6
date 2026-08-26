"""Export AutoCAB proposals as PR-ready skill folders."""

from __future__ import annotations

import json
from pathlib import Path

from autocab.models import SkillProposal


def _skill_markdown(proposal: SkillProposal) -> str:
    matched_skill_lines = "\n".join(
        f"- {match.skill.name} ({match.score:.2f})" for match in proposal.matched_skills
    )
    input_lines = "\n".join(f"- {item}" for item in proposal.inputs)
    output_lines = "\n".join(f"- {item}" for item in proposal.outputs)
    step_lines = "\n".join(f"{index}. {item}" for index, item in enumerate(proposal.steps, start=1))
    validation_lines = "\n".join(f"- {item}" for item in proposal.validation_notes)

    return f"""---
name: {proposal.title}
slug: {proposal.slug}
proposal_type: {proposal.proposal_type}
coverage: {proposal.coverage}
review_status: {proposal.review_status}
---

# {proposal.title}

## Purpose
{proposal.purpose}

## Workflow Family
{proposal.workflow_family}

## Analysts Observed
{", ".join(proposal.analysts)}

## Inputs
{input_lines}

## Outputs
{output_lines}

## Steps
{step_lines}

## Validation Notes
{validation_lines}

## Reference Skills
{matched_skill_lines or "- None"}

## Sanitized Workflow Summary
{proposal.sanitized_summary}

## Reviewer Notes
{proposal.reviewer_notes or "Pending review."}
"""


def export_proposal(proposal: SkillProposal, output_dir: Path) -> Path:
    """Write a proposal to a draft skill folder."""

    export_path = output_dir / proposal.slug
    export_path.mkdir(parents=True, exist_ok=True)
    proposal.export_path = export_path

    (export_path / "SKILL.md").write_text(_skill_markdown(proposal), encoding="utf-8")
    (export_path / "metadata.json").write_text(
        json.dumps(proposal.to_dict(), indent=2),
        encoding="utf-8",
    )
    return export_path
