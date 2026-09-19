"""Build a conservative SkillSpec from sealed session evidence."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Any

from autocab.forge.skill_spec import SCHEMA_VERSION, validateSpec
from autocab.recording.events import DEID_SEALED, SHELL_COMMAND, Event
from autocab.recording.session import Session

from .models import WorkflowError

NON_MATERIAL_PREFIXES = ("session.", "source.", "screen.recording.")


def _slug(value: str) -> str:
    """Return a valid SkillSpec name stem."""

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    slug = re.sub(r"-(?:cbd|std)$", "", slug)
    return (slug or "recorded-workflow")[:56].rstrip("-")


def _event_id(session_id: str, event: Event) -> str:
    """Create a stable evidence ID from the sealed event content."""

    encoded = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()[:12]
    return f"session-{session_id}-event-{event.seq or 0}-{digest}"


def _event_summary(event: Event) -> str:
    """Describe evidence without inventing workflow meaning."""

    command = event.payload.get("command")
    if isinstance(command, str) and command.strip():
        exit_code = event.payload.get("exit_code")
        return f"Recorded command completed with exit code {exit_code}: {command[:240]}"
    for field in ("text", "note", "message", "path"):
        value = event.payload.get(field)
        if isinstance(value, str) and value.strip():
            return f"Recorded {event.type}: {value[:240]}"
    return f"Recorded {event.type} event."


def _executable(command: str) -> str | None:
    """Return the apparent executable while treating shell syntax cautiously."""

    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] in {"cd", "export", "source", "."}:
        return None
    return tokens[0].rsplit("/", 1)[-1]


def build_evidence_snapshot(
    session: Session, events: list[Event]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return full evidence records and SkillSpec evidence summaries."""

    snapshot: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for event in events:
        evidence_id = _event_id(session.session_id, event)
        summary = _event_summary(event)
        snapshot.append(
            {
                "id": evidence_id,
                "session_id": session.session_id,
                "locator": f"events.jsonl#seq={event.seq or 0}",
                "event": event.to_dict(),
                "summary": summary,
            }
        )
        if event.type == DEID_SEALED or event.type.startswith(NON_MATERIAL_PREFIXES):
            continue
        summaries.append(
            {
                "id": evidence_id,
                "source": f"session:{session.session_id}",
                "locator": f"events.jsonl#seq={event.seq or 0}",
                "basis": "observed",
                "confidence": "high",
                "summary": summary,
            }
        )
    return snapshot, summaries


def build_blocked_spec(
    session: Session,
    events: list[Event],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a valid blocked draft that makes uncertainty explicit."""

    evidence_by_locator = {record["locator"]: record["id"] for record in evidence}
    steps: list[dict[str, Any]] = []
    dependencies: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type != SHELL_COMMAND:
            continue
        command = event.payload.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        evidence_id = evidence_by_locator.get(f"events.jsonl#seq={event.seq or 0}")
        if evidence_id is None:
            continue
        executable = _executable(command)
        dependency_names = [executable] if executable else []
        if executable and executable not in dependencies:
            dependencies[executable] = {
                "name": executable,
                "kind": "missing",
                "required": True,
                "evidenceIds": [evidence_id],
                "install": None,
                "environmentPackage": None,
                "versionConstraint": None,
                "sourcePath": None,
                "bundlePath": None,
                "licenseStatus": "unknown",
                "notes": "Observed in a sealed session. Verify source, version, and license.",
            }
        elif executable:
            dependencies[executable]["evidenceIds"].append(evidence_id)
        steps.append(
            {
                "id": f"command-{len(steps) + 1}",
                "summary": f"Run recorded command {len(steps) + 1}.",
                "status": "blocked",
                "basis": "observed",
                "evidenceIds": [evidence_id],
                "existingSkill": None,
                "commandShape": command,
                "dependencies": dependency_names,
                "rationale": "The command was observed, but its inputs and dependency closure need review.",
                "approvalRequired": False,
            }
        )

    if not evidence:
        raise WorkflowError(
            f"Session {session.session_id} has no material events from which to draft a skill."
        )

    title = session.manifest.workflow_family or session.manifest.title or "Recorded workflow"
    spec: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "create",
        "requestedPackaging": "auto",
        "packaging": "cbd",
        "decision": "blocked",
        "name": f"{_slug(title)}-cbd",
        "skillVersion": "0.1.0",
        "title": title,
        "description": f"Reproduce {title}. Use after a reviewer verifies the recorded workflow.",
        "purpose": "Turn a sealed AutoCAB session into a reviewed, evidence-linked skill.",
        "triggers": [f"Repeat the reviewed {title} workflow."],
        "nonTriggers": ["Use before the recorded commands and dependencies are reviewed."],
        "inputs": [],
        "outputs": [],
        "steps": steps,
        "dependencies": list(dependencies.values()),
        "runtimeEnvironment": {
            "manager": "none",
            "python": ">=3.12",
            "channels": [],
            "condaDependencies": [],
            "pipDependencies": [],
            "systemDependencies": [],
            "externalArtifacts": [],
            "containerImage": None,
            "codebaseEnvironmentFile": None,
            "lockStrategy": "none",
            "verified": False,
            "notes": ["A reviewer must verify the runtime and dependency versions."],
        },
        "evidence": evidence,
        "assumptions": [],
        "unresolvedQuestions": [
            {
                "id": "q-input-output-roles",
                "question": "What are the required inputs and expected outputs for this workflow?",
                "blocking": True,
                "neededArtifacts": ["sanitized example inputs", "sanitized expected outputs"],
            },
            {
                "id": "q-dependency-closure",
                "question": "Which dependency versions and licenses reproduce the recorded commands?",
                "blocking": True,
                "neededArtifacts": ["environment lock file", "dependency license records"],
            },
        ],
        "qualityChecks": ["Compare a clean-environment run with the reviewed expected outputs."],
        "failureModes": [
            {
                "trigger": "Required workflow details remain unverified.",
                "response": "Keep the run blocked and request evidence instead of guessing.",
            }
        ],
        "existingSkillCoverage": [],
        "codebase": {"roots": []},
        "updateAssessment": None,
        "licenseDecision": "Unknown until dependencies and source ownership are reviewed.",
        "sourceManifests": [f"session:{session.session_id}/.seal/seal.json"],
    }
    issues = validateSpec(spec)
    if issues:
        raise WorkflowError("Generated SkillSpec is invalid: " + "; ".join(issues))
    return spec
