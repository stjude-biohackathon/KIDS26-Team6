"""Tests for the persistent, human-reviewed Skill Forge lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocab.forge.skill_spec import validateSpec
from autocab.workflow import ForgeState, ForgeWorkflow, InvalidTransition, WorkflowError
from wfrec.events import Event
from wfrec.seal import NotSealed


def _record_command(session, command: str = "python workflow.py input.txt") -> None:
    """Add one representative completed command to a test session."""

    session.writer.append(
        Event(
            source="shell",
            type="shell.command.completed",
            payload={"command": command, "exit_code": 0, "duration_ms": 50},
        )
    )


def _reviewable_spec(path: Path) -> Path:
    """Convert a blocked generated draft into a minimal review-ready STD spec."""

    spec = json.loads(path.read_text(encoding="utf-8"))
    spec["requestedPackaging"] = "std"
    spec["packaging"] = "std"
    spec["decision"] = "novel"
    spec["name"] = spec["name"].removesuffix("-cbd") + "-std"
    spec["unresolvedQuestions"] = []
    spec["dependencies"] = []
    spec["runtimeEnvironment"]["verified"] = True
    spec["runtimeEnvironment"]["notes"] = ["Verified in the review fixture."]
    spec["licenseDecision"] = "No copied third-party source is included."
    for step in spec["steps"]:
        step["status"] = "supported"
        step["dependencies"] = []
        step["rationale"] = "The reviewer confirmed the recorded command."
    assert validateSpec(spec) == []
    reviewed = path.with_name("reviewed-skill-spec.json")
    reviewed.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return reviewed


def test_invalid_forge_transition_is_rejected():
    from autocab.workflow.models import ForgeRun

    run = ForgeRun(run_id="20260918T120000Z-a1b2c3d4", session_ids=["session"])
    with pytest.raises(InvalidTransition, match="Cannot move"):
        run.transition(ForgeState.APPROVED, reason="skip review")


def test_forge_refuses_an_unsealed_session(store):
    session, _ = store.start(title="Unsealed workflow", analyst="analyst")
    _record_command(session)

    with pytest.raises(NotSealed, match="unsealed session"):
        ForgeWorkflow().forge(session.session_id)


def test_forge_persists_full_evidence_and_a_blocked_spec(store, seal_now):
    session, _ = store.start(title="Variant QC", analyst="analyst")
    _record_command(session, "bcftools view input.vcf.gz")
    seal_now(session)

    run = ForgeWorkflow().forge(session.session_id)
    run_dir = session.root.parent.parent / "runs" / run.run_id
    evidence = json.loads((run_dir / "evidence.json").read_text(encoding="utf-8"))
    spec = json.loads((run_dir / "skill-spec.json").read_text(encoding="utf-8"))

    assert run.state == ForgeState.BLOCKED
    assert run.evidence_count == len(evidence)
    assert any(record["event"]["payload"].get("exit_code") == 0 for record in evidence)
    assert any(record["locator"].startswith("events.jsonl#seq=") for record in evidence)
    assert spec["decision"] == "blocked"
    assert spec["steps"][0]["commandShape"] == "bcftools view input.vcf.gz"
    assert spec["dependencies"][0]["kind"] == "missing"
    assert validateSpec(spec) == []


def test_review_approval_and_package_are_separate_steps(store, seal_now):
    session, _ = store.start(title="Reviewed workflow", analyst="analyst")
    _record_command(session)
    seal_now(session)
    workflow = ForgeWorkflow()
    run = workflow.forge(session.session_id)
    run_dir = session.root.parent.parent / "runs" / run.run_id
    reviewed_spec = _reviewable_spec(run_dir / "skill-spec.json")

    reviewed = workflow.review(
        run.run_id,
        reviewer="Reviewer One",
        notes="Inputs and outputs checked.",
        spec_path=reviewed_spec,
    )
    assert reviewed.state == ForgeState.NEEDS_REVIEW
    with pytest.raises(WorkflowError, match="approved before packaging"):
        workflow.package(run.run_id)

    approved = workflow.approve(run.run_id, reviewer="Reviewer Two")
    assert approved.state == ForgeState.APPROVED
    packaged = workflow.package(run.run_id)

    assert packaged.state == ForgeState.PACKAGED
    assert packaged.package_path is not None
    assert (run_dir / packaged.package_path / "SKILL.md").is_file()
    decisions = [
        json.loads(line)
        for line in (run_dir / "reviews.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [decision["kind"] for decision in decisions] == ["review", "approval"]
