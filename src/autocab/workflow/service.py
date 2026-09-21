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
from autocab.recording.events import read_events_sorted, utc_now
from autocab.recording.locking import atomic_write_text
from autocab.recording.seal import require_sealed
from autocab.recording.session import Session

from .builder import (
    RUNTIME_VERIFICATION_QUESTION,
    build_blocked_spec,
    build_evidence_snapshot,
)
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


def _single_line(value: str) -> str:
    """Normalize reviewer-supplied provenance into one schema-safe line."""

    return " ".join(value.split())


def _manual_only_spec(spec: dict[str, Any]) -> bool:
    """Return whether a package deliberately documents only manual actions."""

    environment = spec.get("runtimeEnvironment") or {}
    steps = spec.get("steps") or []
    return (
        bool(steps)
        and all(step.get("status") == "manual" for step in steps)
        and spec.get("dependencies") == []
        and environment.get("manager") == "none"
        and environment.get("lockStrategy") == "none"
        and all(
            environment.get(field) == []
            for field in (
                "condaDependencies",
                "pipDependencies",
                "systemDependencies",
                "externalArtifacts",
            )
        )
        and environment.get("containerImage") is None
        and environment.get("codebaseEnvironmentFile") is None
    )


def _runtime_approval_issue(spec: dict[str, Any]) -> str | None:
    """Return the reproducibility blocker that must stop approval."""

    if _manual_only_spec(spec):
        return None
    environment = spec.get("runtimeEnvironment") or {}
    if environment.get("lockStrategy") == "unresolved":
        return "resolve the runtime lock strategy before approval"
    if environment.get("verified") is not True:
        return "record a successful clean-environment smoke test before approval"
    return None


def _ensure_runtime_question(spec: dict[str, Any]) -> None:
    """Restore the runtime blocker when an older run lacks the new question."""

    questions = spec.setdefault("unresolvedQuestions", [])
    if not any(question.get("id") == RUNTIME_VERIFICATION_QUESTION["id"] for question in questions):
        questions.append(dict(RUNTIME_VERIFICATION_QUESTION))


def _finalize_resolved_spec(spec: dict[str, Any]) -> None:
    """Apply conservative defaults after the final blocking question is resolved."""

    if any(question.get("blocking") is True for question in spec["unresolvedQuestions"]):
        return
    if spec.get("decision") == "blocked":
        spec["decision"] = "novel"
    spec["requestedPackaging"] = "std"
    spec["packaging"] = "std"
    name = str(spec.get("name") or "recorded-workflow")
    for suffix in ("-cbd", "-std"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    spec["name"] = f"{name}-std"
    spec["codebase"] = {"roots": []}
    managed_dependencies = bool(spec.get("dependencies"))
    generated_rationales = {
        "The command was observed, but its inputs and dependency closure need review.",
        "The activity was observed, but its inputs and dependency closure need review.",
    }
    for step in spec.get("steps") or []:
        if step.get("status") != "blocked" or step.get("rationale") not in generated_rationales:
            continue
        step["status"] = "supported" if managed_dependencies else "manual"
        step["rationale"] = (
            "The command was observed and its dependencies were reviewed."
            if managed_dependencies
            else "The command was observed and remains a manual action in the user environment."
        )


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
            runtime_issue = _runtime_approval_issue(spec)
            if runtime_issue:
                raise WorkflowError(f"Runtime verification is incomplete: {runtime_issue}.")
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

    def verify_runtime(
        self,
        run_id: str,
        *,
        reviewer: str,
        result: str,
        environment: str,
        platform: str,
        smoke_test: str,
        notes: str,
        evidence_ref: str = "",
    ) -> ForgeRun:
        """Record a human-observed runtime check without executing captured commands."""

        if result not in {"passed", "failed", "not_run"}:
            raise ValueError("Runtime verification result must be passed, failed, or not_run.")
        normalized = {
            "environment": _single_line(environment),
            "platform": _single_line(platform),
            "smoke_test": _single_line(smoke_test),
            "notes": _single_line(notes),
            "evidence_ref": _single_line(evidence_ref),
        }
        if not normalized["notes"]:
            raise ValueError("Runtime verification notes are required.")
        if result == "passed":
            missing = [
                label
                for field, label in (
                    ("environment", "environment or container"),
                    ("platform", "platform"),
                    ("smoke_test", "smoke test"),
                )
                if not normalized[field]
            ]
            if missing:
                raise ValueError(
                    "Passed runtime verification requires: " + ", ".join(missing) + "."
                )

        with self.store.lock(run_id) as run_dir:
            run = self.store.load(run_id)
            if run.state != ForgeState.BLOCKED:
                raise WorkflowError(
                    f"Run {run_id} must be blocked to record runtime verification, "
                    f"not {run.state}."
                )
            spec = _load_spec(run_dir / "skill-spec.json")
            questions = spec.get("unresolvedQuestions") or []
            if result == "passed" and any(
                question.get("id") == "q-dependency-closure" for question in questions
            ):
                raise WorkflowError(
                    "Resolve dependency versions and licenses before verifying the runtime."
                )
            runtime = spec["runtimeEnvironment"]
            if result == "passed" and runtime.get("lockStrategy") == "unresolved":
                raise WorkflowError(
                    "Resolve the runtime lock strategy before recording a passing verification."
                )

            evidence_id = f"review-{run_id}-runtime-verification"
            detail_parts = [
                f"result={result}",
                f"environment={normalized['environment'] or 'not recorded'}",
                f"platform={normalized['platform'] or 'not recorded'}",
                f"smoke_test={normalized['smoke_test'] or 'not run'}",
            ]
            if normalized["evidence_ref"]:
                detail_parts.append(f"evidence={normalized['evidence_ref']}")
            spec["evidence"] = [
                record for record in spec["evidence"] if record.get("id") != evidence_id
            ] + [
                {
                    "id": evidence_id,
                    "source": "runtime-review",
                    "locator": f"forge-run:{run_id}#runtime-verification",
                    "basis": "user_confirmed",
                    "confidence": "high",
                    "summary": "Runtime verification recorded: " + "; ".join(detail_parts),
                }
            ]
            runtime["verified"] = result == "passed"
            runtime["notes"] = [
                f"Verification result: {result}.",
                f"Environment or container: {normalized['environment'] or 'not recorded'}.",
                f"Platform: {normalized['platform'] or 'not recorded'}.",
                f"Smoke test: {normalized['smoke_test'] or 'not run'}.",
                f"Reviewer notes: {normalized['notes']}",
            ]
            if normalized["evidence_ref"]:
                runtime["notes"].append(
                    f"Verification evidence: {normalized['evidence_ref']}"
                )
            if result == "passed":
                spec["unresolvedQuestions"] = [
                    question
                    for question in questions
                    if question.get("id") != RUNTIME_VERIFICATION_QUESTION["id"]
                ]
                _finalize_resolved_spec(spec)
            else:
                _ensure_runtime_question(spec)

            issues = validateSpec(spec)
            if issues:
                raise WorkflowError("Runtime verification failed: " + "; ".join(issues))
            blockers = [
                question
                for question in spec["unresolvedQuestions"]
                if question.get("blocking") is True
            ]
            next_state = ForgeState.BLOCKED if blockers else ForgeState.NEEDS_REVIEW
            if next_state == ForgeState.NEEDS_REVIEW and spec["decision"] not in RENDERABLE_DECISIONS:
                raise WorkflowError("Resolve the remaining SkillSpec decisions before approval.")
            atomic_write_text(
                run_dir / "skill-spec.json",
                json.dumps(spec, indent=2, sort_keys=True) + "\n",
            )
            run.unresolved_count = len(spec["unresolvedQuestions"])
            run.transition(next_state, reason="Reviewer recorded runtime verification.")
            self.store.append_review(
                run_id,
                {
                    "kind": "runtime_verification",
                    "reviewer": reviewer,
                    "notes": normalized["notes"],
                    "at": utc_now(),
                    "result": result,
                    "environment": normalized["environment"],
                    "platform": normalized["platform"],
                    "smoke_test": normalized["smoke_test"],
                    "evidence_ref": normalized["evidence_ref"],
                },
            )
            self.store.save(run)
            return run

    def reopen(self, run_id: str, *, reviewer: str, notes: str = "") -> ForgeRun:
        """Invalidate current approval and return an un-packaged run to review."""

        with self.store.lock(run_id) as run_dir:
            run = self.store.load(run_id)
            if run.state != ForgeState.APPROVED:
                raise WorkflowError(
                    f"Run {run_id} must be approved to reopen it, not {run.state}."
                )
            spec = _load_spec(run_dir / "skill-spec.json")
            if _runtime_approval_issue(spec):
                _ensure_runtime_question(spec)
                atomic_write_text(
                    run_dir / "skill-spec.json",
                    json.dumps(spec, indent=2, sort_keys=True) + "\n",
                )
            previous_approval = {
                "reviewer": run.approved_by,
                "at": run.approved_at,
            }
            run.approved_by = None
            run.approved_at = None
            run.unresolved_count = len(spec["unresolvedQuestions"])
            run.transition(ForgeState.BLOCKED, reason="Approved run reopened for review.")
            self.store.append_review(
                run_id,
                {
                    "kind": "reopen",
                    "reviewer": reviewer,
                    "notes": _single_line(notes),
                    "at": utc_now(),
                    "result": ForgeState.BLOCKED.value,
                    "invalidated_approval": previous_approval,
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
                forge_log = render_dir / "logs" / "skill-forge.log"
                if forge_log.is_file():
                    retained_log = run_dir / "logs" / "skill-forge.log"
                    retained_log.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(forge_log, retained_log)
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
