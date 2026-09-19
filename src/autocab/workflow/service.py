"""Services for forging, reviewing, approving, and packaging skills."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from autocab.forge.package_validator import validatePackage
from autocab.forge.renderer import runForge
from autocab.forge.skill_spec import (
    RENDERABLE_DECISIONS,
    SkillSpecError,
    loadSpec,
    validateSpec,
)
from wfrec.events import read_events_sorted, utc_now
from wfrec.locking import atomic_write_text
from wfrec.seal import require_sealed
from wfrec.session import Session

from .builder import build_blocked_spec, build_evidence_snapshot
from .models import ForgeRun, ForgeState, WorkflowError
from .store import RunStore


def _new_run_id() -> str:
    """Return a sortable run ID with collision resistance."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:8]}"


def _seal_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Keep the integrity facts needed to trace a run to its source."""

    targets = record.get("targets") or {}
    return {
        "assurance": record.get("assurance"),
        "accepted_partial": bool(record.get("accepted_partial")),
        "sealed_at": record.get("sealed_at") or record.get("completed_at"),
        "targets": {
            str(name): {"after_sha256": values.get("after_sha256")}
            for name, values in targets.items()
            if isinstance(values, dict)
        },
    }


def _load_spec(path: Path) -> dict[str, Any]:
    """Translate a malformed persisted spec into a workflow-level error."""

    try:
        return loadSpec(path)
    except SkillSpecError as exc:
        raise WorkflowError(str(exc)) from exc


class ForgeWorkflow:
    """Coordinate the durable, human-reviewed session-to-skill workflow."""

    def __init__(self, store: RunStore | None = None) -> None:
        self.store = store or RunStore()

    def forge(self, session_id: str) -> ForgeRun:
        """Snapshot one sealed session and create a blocked SkillSpec draft."""

        session = Session.load(session_id)
        seal = require_sealed(session.root, what="Skill forging")
        events = read_events_sorted(session.root / "events.jsonl")
        snapshot, evidence = build_evidence_snapshot(session, events)
        spec = build_blocked_spec(session, events, evidence)
        run = ForgeRun(
            run_id=_new_run_id(),
            session_ids=[session_id],
            evidence_count=len(snapshot),
            unresolved_count=len(spec["unresolvedQuestions"]),
            source_seals={session_id: _seal_summary(seal)},
        )
        run.transition(ForgeState.BLOCKED, reason="Generated draft requires human review.")
        self.store.create(
            run,
            artifacts={"evidence.json": snapshot, "skill-spec.json": spec},
        )
        return run

    def review(
        self,
        run_id: str,
        *,
        reviewer: str,
        notes: str,
        spec_path: Path | None = None,
        spec: dict[str, Any] | None = None,
    ) -> ForgeRun:
        """Validate reviewer edits and mark whether blockers remain."""

        if spec_path is not None and spec is not None:
            raise ValueError("Provide either spec_path or spec, not both.")
        with self.store.lock(run_id) as run_dir:
            run = self.store.load(run_id)
            if run.state in {ForgeState.APPROVED, ForgeState.PACKAGED}:
                raise WorkflowError(f"Run {run_id} cannot be reviewed in state {run.state}.")
            reviewed_spec = (
                spec if spec is not None else _load_spec(spec_path or run_dir / "skill-spec.json")
            )
            issues = validateSpec(reviewed_spec)
            if issues:
                raise WorkflowError("SkillSpec review failed: " + "; ".join(issues))
            blockers = [
                question
                for question in reviewed_spec["unresolvedQuestions"]
                if question.get("blocking") is True
            ]
            next_state = ForgeState.BLOCKED if blockers else ForgeState.NEEDS_REVIEW
            if (
                next_state == ForgeState.NEEDS_REVIEW
                and reviewed_spec["decision"] not in RENDERABLE_DECISIONS
            ):
                raise WorkflowError("A review-ready skill must use decision 'compose' or 'novel'.")
            atomic_write_text(
                run_dir / "skill-spec.json",
                json.dumps(reviewed_spec, indent=2, sort_keys=True) + "\n",
            )
            run.unresolved_count = len(reviewed_spec["unresolvedQuestions"])
            run.transition(next_state, reason="Reviewer validated the SkillSpec.")
            self.store.append_review(
                run_id,
                {
                    "kind": "review",
                    "reviewer": reviewer,
                    "notes": notes,
                    "at": utc_now(),
                    "result": next_state.value,
                    "blocking_questions": len(blockers),
                },
            )
            self.store.save(run)
            return run

    def approve(self, run_id: str, *, reviewer: str, notes: str = "") -> ForgeRun:
        """Record a separate, explicit approval decision."""

        with self.store.lock(run_id) as run_dir:
            run = self.store.load(run_id)
            if run.state != ForgeState.NEEDS_REVIEW:
                raise WorkflowError(
                    f"Run {run_id} must be in needs_review before approval, not {run.state}."
                )
            spec = _load_spec(run_dir / "skill-spec.json")
            issues = validateSpec(spec)
            if issues:
                raise WorkflowError("SkillSpec approval failed: " + "; ".join(issues))
            if spec["decision"] not in RENDERABLE_DECISIONS:
                raise WorkflowError(
                    "Only a compose or novel decision can be approved for packaging."
                )
            if any(question.get("blocking") for question in spec["unresolvedQuestions"]):
                raise WorkflowError("Blocking questions must be resolved before approval.")
            approved_at = utc_now()
            run.transition(ForgeState.APPROVED, reason="Reviewer explicitly approved the run.")
            run.approved_by = reviewer
            run.approved_at = approved_at
            self.store.append_review(
                run_id,
                {
                    "kind": "approval",
                    "reviewer": reviewer,
                    "notes": notes,
                    "at": approved_at,
                    "result": ForgeState.APPROVED.value,
                },
            )
            self.store.save(run)
            return run

    def package(self, run_id: str) -> ForgeRun:
        """Render and validate the approved skill package."""

        with self.store.lock(run_id) as run_dir:
            run = self.store.load(run_id)
            if run.state != ForgeState.APPROVED:
                raise WorkflowError(
                    f"Run {run_id} must be approved before packaging, not {run.state}."
                )
            spec_path = run_dir / "skill-spec.json"
            spec = _load_spec(spec_path)
            issues = validateSpec(spec)
            if issues:
                raise WorkflowError("SkillSpec packaging failed: " + "; ".join(issues))
            renderer_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            with TemporaryDirectory(prefix="autocab-forge-") as temporary:
                render_dir = Path(temporary) / "render"
                result = runForge(
                    argparse.Namespace(
                        spec=str(spec_path),
                        validateOnly=False,
                        outputDir=str(render_dir),
                        existingSkillDir=None,
                        allowCodeCopy=False,
                        runId=renderer_id,
                        agentRequestFile=None,
                        agentWorkflowFile=None,
                    )
                )
                if result != 0:
                    raise WorkflowError("Skill renderer rejected the approved SkillSpec.")
                rendered = render_dir / "proposal" / spec["name"]
                report = validatePackage(
                    rendered,
                    expectedPackaging=spec["packaging"],
                    strict=True,
                    allowProposed=False,
                )
                if not report["valid"]:
                    raise WorkflowError(
                        "Generated package failed validation: " + "; ".join(report["errors"])
                    )
                package_dir = run_dir / "package"
                if package_dir.exists():
                    raise WorkflowError(f"Package directory already exists: {package_dir}")
                shutil.copytree(rendered, package_dir / spec["name"])
            self.store.write_json(run_id, "validation.json", report)
            run.package_path = str(Path("package") / spec["name"])
            run.transition(ForgeState.PACKAGED, reason="Approved package passed strict validation.")
            self.store.save(run)
            return run
