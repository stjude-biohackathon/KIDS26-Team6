"""Tests for the persistent, human-reviewed Skill Forge lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autocab.forge.skill_spec import validateSpec
from autocab.workflow.dependency_versions import DetectedVersion
from autocab.workflow import ForgeState, ForgeWorkflow, InvalidTransition, WorkflowError
from autocab.recording.events import Event
from autocab.recording.seal import NotSealed


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
    assert [question["id"] for question in spec["unresolvedQuestions"]] == [
        "q-input-output-roles",
        "q-dependency-closure",
        "q-runtime-verification",
    ]


def test_forge_prefills_detected_dependency_version(store, seal_now, monkeypatch):
    """Dependency review should start with locally detected version evidence."""

    session, _ = store.start(title="Versioned workflow", analyst="analyst")
    _record_command(session, "git status")
    seal_now(session)
    monkeypatch.setattr(
        "autocab.workflow.builder.detect_dependency_version",
        lambda executable: DetectedVersion("2.51.0", f"{executable} --version"),
    )

    run = ForgeWorkflow().forge(session.session_id)
    run_dir = session.root.parent.parent / "runs" / run.run_id
    spec = json.loads((run_dir / "skill-spec.json").read_text(encoding="utf-8"))

    dependency = spec["dependencies"][0]
    assert dependency["versionConstraint"] == "2.51.0"
    assert "git --version" in dependency["notes"]
    assert spec["runtimeEnvironment"]["verified"] is False
    assert validateSpec(spec) == []


def test_runtime_verification_unblocks_review_and_records_evidence(store, seal_now):
    session, _ = store.start(title="Verified workflow", analyst="analyst")
    _record_command(session)
    seal_now(session)
    workflow = ForgeWorkflow()
    run = workflow.forge(session.session_id)
    run_dir = workflow.store.run_dir(run.run_id)
    reviewed_path = _reviewable_spec(run_dir / "skill-spec.json")
    spec = json.loads(reviewed_path.read_text(encoding="utf-8"))
    spec["runtimeEnvironment"]["verified"] = False
    spec["runtimeEnvironment"]["notes"] = ["Clean-environment verification is pending."]
    spec["decision"] = "blocked"
    spec["unresolvedQuestions"] = [
        {
            "id": "q-runtime-verification",
            "question": "How was the runtime verified?",
            "blocking": True,
            "neededArtifacts": ["smoke-test result"],
        }
    ]
    reviewed_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    assert workflow.review(
        run.run_id,
        reviewer="Analyst",
        notes="Resolved the workflow contract.",
        spec_path=reviewed_path,
    ).state == ForgeState.BLOCKED

    verified = workflow.verify_runtime(
        run.run_id,
        reviewer="Verifier",
        result="passed",
        environment="fresh test environment",
        platform="linux-64",
        smoke_test="python workflow.py fixture.txt",
        notes="Expected output was reproduced.",
        evidence_ref="verification/run.log",
    )

    assert verified.state == ForgeState.NEEDS_REVIEW
    persisted = json.loads((run_dir / "skill-spec.json").read_text(encoding="utf-8"))
    assert persisted["runtimeEnvironment"]["verified"] is True
    assert persisted["unresolvedQuestions"] == []
    assert persisted["decision"] == "novel"
    assert persisted["evidence"][-1]["source"] == "runtime-review"
    reviews = [
        json.loads(line)
        for line in (run_dir / "reviews.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert reviews[-1]["kind"] == "runtime_verification"
    assert reviews[-1]["result"] == "passed"


def test_approval_rejects_unverified_executable_runtime(store, seal_now):
    session, _ = store.start(title="Unverified workflow", analyst="analyst")
    _record_command(session)
    seal_now(session)
    workflow = ForgeWorkflow()
    run = workflow.forge(session.session_id)
    run_dir = workflow.store.run_dir(run.run_id)
    reviewed_path = _reviewable_spec(run_dir / "skill-spec.json")
    spec = json.loads(reviewed_path.read_text(encoding="utf-8"))
    spec["runtimeEnvironment"]["verified"] = False
    reviewed_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    reviewed = workflow.review(
        run.run_id,
        reviewer="Analyst",
        notes="Removed all declared blockers without runtime evidence.",
        spec_path=reviewed_path,
    )
    assert reviewed.state == ForgeState.NEEDS_REVIEW

    with pytest.raises(WorkflowError, match="clean-environment smoke test"):
        workflow.approve(run.run_id, reviewer="Maintainer")


def test_reopen_invalidates_approval_and_preserves_audit_history(store, seal_now):
    session, _ = store.start(title="Reopened workflow", analyst="analyst")
    _record_command(session)
    seal_now(session)
    workflow = ForgeWorkflow()
    run = workflow.forge(session.session_id)
    run_dir = workflow.store.run_dir(run.run_id)
    reviewed_path = _reviewable_spec(run_dir / "skill-spec.json")
    workflow.review(
        run.run_id,
        reviewer="Analyst",
        notes="Prepared for approval.",
        spec_path=reviewed_path,
    )
    approved = workflow.approve(run.run_id, reviewer="Maintainer")
    assert approved.approved_by == "Maintainer"

    reopened = workflow.reopen(
        run.run_id,
        reviewer="Maintainer",
        notes="Correct the runtime evidence.",
    )

    assert reopened.state == ForgeState.BLOCKED
    assert reopened.approved_by is None
    assert reopened.approved_at is None
    assert reopened.history[-1]["from"] == "approved"
    reviews = [
        json.loads(line)
        for line in (run_dir / "reviews.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert reviews[-1]["kind"] == "reopen"
    assert reviews[-1]["invalidated_approval"]["reviewer"] == "Maintainer"


def test_forge_builds_steps_from_agent_tools_without_trusting_agent_prose(store, seal_now):
    session, _ = store.start(title="Agent workflow", analyst="analyst")
    session.writer.extend(
        [
            Event(
                source="agents",
                type="agent.message",
                payload={"tool": "codex", "role": "user", "text": "Run the analysis."},
            ),
            Event(
                source="agents",
                type="agent.message",
                payload={
                    "tool": "codex",
                    "role": "assistant",
                    "text": "I changed every result successfully.",
                },
            ),
            Event(
                source="agents",
                type="agent.tool.completed",
                payload={
                    "agent": "codex",
                    "tool_name": "exec_command",
                    "category": "command",
                    "command": "python workflow.py input.txt",
                    "exit_code": 0,
                    "session_id": "thread-1",
                },
            ),
        ]
    )
    seal_now(session)

    run = ForgeWorkflow().forge(session.session_id)
    run_dir = session.root.parent.parent / "runs" / run.run_id
    spec = json.loads((run_dir / "skill-spec.json").read_text(encoding="utf-8"))

    assert len(spec["steps"]) == 1
    assert spec["steps"][0]["commandShape"] == "python workflow.py input.txt"
    assert spec["dependencies"][0]["name"] == "python"
    assert len(spec["steps"][0]["evidenceIds"]) == 1
    messages = [record for record in spec["evidence"] if "agent.message" in record["summary"]]
    assert [record["confidence"] for record in messages] == ["medium", "low"]
    assert validateSpec(spec) == []


def test_forge_deduplicates_matching_shell_and_agent_commands(store, seal_now):
    session, _ = store.start(title="Deduplicated workflow", analyst="analyst")
    timestamp = "2030-01-01T00:00:01.000Z"
    session.writer.extend(
        [
            Event(
                source="shell",
                type="shell.command.completed",
                ts=timestamp,
                payload={"command": "python workflow.py", "cwd": "/project", "exit_code": 0},
            ),
            Event(
                source="agents",
                type="agent.tool.completed",
                ts="2030-01-01T00:00:02.000Z",
                payload={
                    "agent": "codex",
                    "tool_name": "exec_command",
                    "category": "command",
                    "command": "python  workflow.py",
                    "cwd": "/project",
                    "session_id": "thread-1",
                    "exit_code": 0,
                },
            ),
        ]
    )
    seal_now(session)

    run = ForgeWorkflow().forge(session.session_id)
    run_dir = session.root.parent.parent / "runs" / run.run_id
    spec = json.loads((run_dir / "skill-spec.json").read_text(encoding="utf-8"))

    assert len(spec["steps"]) == 1
    assert len(spec["steps"][0]["evidenceIds"]) == 2
    assert len(spec["dependencies"][0]["evidenceIds"]) == 2


def test_forge_links_agent_edits_and_keeps_external_tools_reviewable(store, seal_now):
    session, _ = store.start(title="Agent edits", analyst="analyst")
    session.writer.extend(
        [
            Event(
                source="agents",
                type="agent.tool.completed",
                ts="2030-01-01T00:00:01.000Z",
                payload={
                    "agent": "codex",
                    "tool_name": "apply_patch",
                    "category": "edit",
                    "session_id": "thread-1",
                },
            ),
            Event(
                source="files",
                type="file.diff",
                ts="2030-01-01T00:00:02.000Z",
                payload={"path": "workflow.py", "added": 5, "deleted": 1},
            ),
            Event(
                source="agents",
                type="agent.tool.completed",
                ts="2030-01-01T00:00:03.000Z",
                payload={
                    "agent": "codex",
                    "tool_name": "web_search",
                    "category": "search",
                    "session_id": "thread-1",
                },
            ),
            Event(
                source="agents",
                type="agent.tool.completed",
                ts="2030-01-01T00:00:04.000Z",
                payload={"agent": "codex", "tool_name": "wait", "category": "wait"},
            ),
        ]
    )
    seal_now(session)

    run = ForgeWorkflow().forge(session.session_id)
    run_dir = session.root.parent.parent / "runs" / run.run_id
    spec = json.loads((run_dir / "skill-spec.json").read_text(encoding="utf-8"))

    assert [step["summary"] for step in spec["steps"]] == [
        "Repeat recorded edits with apply_patch.",
        "Review recorded web_search activity.",
    ]
    assert len(spec["steps"][0]["evidenceIds"]) == 2
    assert all(step["commandShape"] is None for step in spec["steps"])
    assert spec["dependencies"] == []
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
