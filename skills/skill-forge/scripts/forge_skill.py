#!/usr/bin/env python3
"""Validate a SkillSpec and deterministically render a staged skill proposal."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
from string import Template
import sys
from typing import Any

from skill_spec import (
    RENDERABLE_DECISIONS,
    SkillSpecError,
    jsonScalar,
    loadSpec,
    markdownList,
    safeRelativePath,
    validateSpec,
)


FORGE_VERSION = "0.1.0"
RUN_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z$")


class ForgeError(Exception):
    """Represent an actionable staging or rendering failure."""


def utcNow() -> str:
    """Return the current UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def defaultRunId() -> str:
    """Return a UTC run identifier."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def writeJson(path: Path, value: Any) -> None:
    """Write deterministic indented JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fileSha256(path: Path) -> str:
    """Return SHA-256 for a regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensureEmptyRunDir(path: Path, skillRoot: Path) -> None:
    """Create an empty run directory outside the skill source package."""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(skillRoot.resolve())
        raise ForgeError(
            "Run output must be outside the skill-forge source package: "
            f"{resolved}"
        )
    except ValueError:
        pass
    if resolved.exists():
        if not resolved.is_dir():
            raise ForgeError(f"--outputDir is not a directory: {resolved}")
        if any(resolved.iterdir()):
            raise ForgeError(
                f"--outputDir must be empty; refusing to overwrite: {resolved}"
            )
    else:
        resolved.mkdir(parents=True, exist_ok=False)


def configureLogging(runDir: Path) -> logging.Logger:
    """Configure console and run-scoped file logging."""
    logger = logging.getLogger("skill-forge")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logsDir = runDir / "logs"
    logsDir.mkdir(parents=True, exist_ok=True)
    fileHandler = logging.FileHandler(
        logsDir / "skill-forge.log", encoding="utf-8"
    )
    fileHandler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(fileHandler)
    return logger


def closeLogging(logger: logging.Logger) -> None:
    """Flush and close run-scoped logging handlers."""
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    logger.handlers.clear()


def appendCommandLog(runDir: Path, runId: str, args: argparse.Namespace) -> None:
    """Append a privacy-minimized command record."""
    command = [
        "python",
        "scripts/forge_skill.py",
        "--spec",
        f"<SPEC>/{Path(args.spec).name}",
        "--outputDir",
        "<RUN_DIR>",
        "--runId",
        runId,
    ]
    if args.allowCodeCopy:
        command.append("--allowCodeCopy")
    if args.existingSkillDir:
        command.extend(
            ["--existingSkillDir", f"<EXISTING_SKILL>/{Path(args.existingSkillDir).name}"]
        )
    with (runDir / "logs" / "commands.log").open("a", encoding="utf-8") as handle:
        handle.write(f"[{utcNow()}] run_id={runId}\n")
        handle.write(" ".join(command) + "\n\n")


def copyTextInput(sourceValue: str | None, destination: Path, fallback: str) -> None:
    """Copy a caller-prepared text record or write a privacy-safe fallback."""
    if sourceValue:
        source = Path(sourceValue).expanduser()
        if not source.is_file() or source.is_symlink():
            raise ForgeError(f"Text provenance input is not a regular file: {source}")
        text = source.read_text(encoding="utf-8")
    else:
        text = fallback
    destination.write_text(text.rstrip() + "\n", encoding="utf-8")


def renderIo(records: list[dict[str, Any]], required: bool) -> str:
    """Render required or optional I/O role bullets."""
    selected = [record for record in records if record["required"] is required]
    if not selected:
        return "None."
    lines = []
    for record in selected:
        evidence = ", ".join(record["evidenceIds"])
        lines.append(
            f"- **`{record['name']}`**: {record['description']} "
            f"(evidence: {evidence})"
        )
    return "\n".join(lines)


def renderTriggers(values: list[str], emptyText: str) -> str:
    """Render realistic trigger/non-trigger language."""
    return markdownList(values, emptyText)


def renderDependencies(dependencies: list[dict[str, Any]]) -> str:
    """Render dependencies without exposing local custom source paths."""
    if not dependencies:
        return "No executable dependencies."
    lines: list[str] = []
    for dependency in dependencies:
        details = [dependency["kind"]]
        if dependency["versionConstraint"]:
            details.append(f"version {dependency['versionConstraint']}")
        if dependency["install"]:
            details.append(f"install: `{dependency['install']}`")
        if dependency["bundlePath"]:
            details.append(f"bundled as `{dependency['bundlePath']}`")
        if dependency["notes"]:
            details.append(dependency["notes"])
        requirement = "required" if dependency["required"] else "optional"
        lines.append(
            f"- **{dependency['name']}** ({requirement}): " + "; ".join(details)
        )
    return "\n".join(lines)


def renderWorkflow(steps: list[dict[str, Any]]) -> str:
    """Render ordered workflow steps with evidence and approval state."""
    if not steps:
        return "No operational steps were established."
    lines: list[str] = []
    for index, step in enumerate(steps, start=1):
        lines.append(
            f"{index}. **{step['summary']}** "
            f"(`{step['status']}`, basis: `{step['basis']}`)."
        )
        if step["existingSkill"]:
            lines.append(f"   - Invoke existing skill: `{step['existingSkill']}`.")
        if step["commandShape"]:
            lines.append(f"   - Command shape: `{step['commandShape']}`")
        if step["dependencies"]:
            lines.append(
                "   - Dependencies: "
                + ", ".join(f"`{name}`" for name in step["dependencies"])
                + "."
            )
        if step["evidenceIds"]:
            lines.append(
                "   - Evidence: " + ", ".join(step["evidenceIds"]) + "."
            )
        if step["status"] == "proposed":
            lines.append(
                "   - **Pending approval:** do not execute or treat this inferred "
                "step as established behavior until the user approves it."
            )
        if step["rationale"]:
            lines.append(f"   - Rationale: {step['rationale']}")
    return "\n".join(lines)


def renderOutputs(outputs: list[dict[str, Any]]) -> str:
    """Render output roles."""
    if not outputs:
        return "No output contract was established."
    return "\n".join(
        f"- **`{record['name']}`**: {record['description']}" for record in outputs
    )


def renderFailureModes(failures: list[dict[str, str]]) -> str:
    """Render failure/response pairs."""
    if not failures:
        return "- Unexpected or unclassified failure: stop and report the evidence."
    return "\n".join(
        f"- **{failure['trigger']}** — {failure['response']}" for failure in failures
    )


def renderCodebaseRequirements(codebase: dict[str, Any]) -> str:
    """Render CBD sentinels without machine-local roots."""
    roots = codebase.get("roots", [])
    if not roots:
        return (
            "Required roots and sentinels are unresolved. Do not run until the "
            "blocking location question is resolved."
        )
    lines = ["Expected configured codebase evidence:"]
    for index, root in enumerate(roots, start=1):
        sentinels = ", ".join(f"`{value}`" for value in root["sentinels"])
        remote = (
            f"; expected local origin `{root['gitRemote']}`"
            if root.get("gitRemote")
            else ""
        )
        lines.append(f"- Root {index}: sentinels {sentinels}{remote}.")
    return "\n".join(lines)


def compatibilityText(spec: dict[str, Any]) -> str:
    """Build a concise compatibility summary."""
    public = [
        dependency["name"]
        for dependency in spec["dependencies"]
        if dependency["kind"] in {"public_tool", "public_pipeline"}
    ]
    publicText = ", ".join(public) if public else "no additional public tools"
    if spec["packaging"] == "cbd":
        return (
            "Requires local filesystem access, a validated AutoCAB CBD codebase "
            f"configuration, and {publicText}."
        )
    return (
        "Requires local filesystem access, all custom helpers bundled in this "
        f"skill, and {publicText}."
    )


def fillSkillTemplate(spec: dict[str, Any], assetsDir: Path) -> str:
    """Fill the deterministic CBD or STD SKILL.md template."""
    templateName = (
        "cbd-skill.template.md"
        if spec["packaging"] == "cbd"
        else "std-skill.template.md"
    )
    template = Template((assetsDir / templateName).read_text(encoding="utf-8"))
    values = {
        "name": spec["name"],
        "description_yaml": jsonScalar(spec["description"]),
        "compatibility_yaml": jsonScalar(compatibilityText(spec)),
        "review_date": datetime.now(timezone.utc).date().isoformat(),
        "title": spec["title"],
        "purpose": spec["purpose"],
        "when_to_use": renderTriggers(
            spec["triggers"], "Use only for the purpose stated above."
        ),
        "when_not_to_use": renderTriggers(
            spec["nonTriggers"], "Do not use outside the stated purpose."
        ),
        "required_inputs": renderIo(spec["inputs"], True),
        "optional_inputs": renderIo(spec["inputs"], False),
        "dependencies": renderDependencies(spec["dependencies"]),
        "workflow": renderWorkflow(spec["steps"]),
        "outputs": renderOutputs(spec["outputs"]),
        "quality_checks": markdownList(
            spec["qualityChecks"], "- No quality checks were established."
        ),
        "failure_modes": renderFailureModes(spec["failureModes"]),
        "codebase_requirements": renderCodebaseRequirements(spec["codebase"]),
    }
    return template.substitute(values).rstrip() + "\n"


def evidenceSummary(spec: dict[str, Any]) -> str:
    """Render evidence counts by basis and confidence."""
    basis = Counter(record["basis"] for record in spec["evidence"])
    confidence = Counter(record["confidence"] for record in spec["evidence"])
    basisText = ", ".join(f"{key}={basis[key]}" for key in sorted(basis)) or "none"
    confidenceText = (
        ", ".join(f"{key}={confidence[key]}" for key in sorted(confidence)) or "none"
    )
    return f"- By basis: {basisText}\n- By confidence: {confidenceText}"


def coverageSummary(spec: dict[str, Any]) -> str:
    """Render inspected existing-skill coverage."""
    if not spec["existingSkillCoverage"]:
        return "No existing skill coverage was recorded."
    lines: list[str] = []
    for record in spec["existingSkillCoverage"]:
        covered = ", ".join(record["actionsCovered"]) or "none"
        gaps = ", ".join(record["gaps"]) or "none"
        lines.append(
            f"- **{record['skill']}**: covered [{covered}]; gaps [{gaps}]; "
            f"handoff compatible: {str(record['handoffCompatible']).lower()}."
        )
    return "\n".join(lines)


def selectedSteps(spec: dict[str, Any], statuses: set[str]) -> str:
    """Render review bullets for selected step statuses."""
    selected = [step for step in spec["steps"] if step["status"] in statuses]
    if not selected:
        return "None."
    lines: list[str] = []
    for step in selected:
        command = f" Command: `{step['commandShape']}`." if step["commandShape"] else ""
        lines.append(
            f"- **{step['id']}** ({step['status']}): {step['summary']}.{command} "
            f"Rationale: {step['rationale'] or 'not supplied'}"
        )
    return "\n".join(lines)


def dependencyReview(spec: dict[str, Any], allowCodeCopy: bool) -> str:
    """Render dependency closure and code-copy state."""
    lines = [renderDependencies(spec["dependencies"])]
    customFiles = [
        dependency
        for dependency in spec["dependencies"]
        if dependency["kind"] == "custom_standalone"
    ]
    if customFiles:
        state = (
            "Code-copy approval flag supplied."
            if allowCodeCopy
            else "Custom code has not been copied; --allowCodeCopy was not supplied."
        )
        lines.extend(["", state])
    return "\n".join(lines)


def unresolvedReview(spec: dict[str, Any]) -> str:
    """Render exact unresolved questions and requested artifacts."""
    if not spec["unresolvedQuestions"]:
        return "None."
    lines: list[str] = []
    for question in spec["unresolvedQuestions"]:
        artifacts = ", ".join(question["neededArtifacts"]) or "no artifact named"
        severity = "blocking" if question["blocking"] else "non-blocking"
        lines.append(
            f"- **{question['id']}** ({severity}): {question['question']} "
            f"Needed: {artifacts}."
        )
    return "\n".join(lines)


def updateReview(spec: dict[str, Any]) -> str:
    """Render update delta or create status."""
    assessment = spec["updateAssessment"]
    if assessment is None:
        return "Create operation; no existing skill migration is proposed."
    return (
        f"- Target: `{assessment['targetSkill']}`\n"
        f"- Material delta: {str(assessment['materialDelta']).lower()}\n"
        f"- Summary: {assessment['summary']}\n"
        f"- Migration: `{assessment['migrationSuggested']}`; approved: "
        f"{str(assessment['migrationApproved']).lower()}"
    )


def renderReview(
    spec: dict[str, Any],
    assetsDir: Path,
    proposalRendered: bool,
    allowCodeCopy: bool,
) -> str:
    """Fill REVIEW.md."""
    template = Template(
        (assetsDir / "review.template.md").read_text(encoding="utf-8")
    )
    return (
        template.substitute(
            {
                "name": spec["name"],
                "operation": spec["operation"],
                "requested_packaging": spec["requestedPackaging"],
                "packaging": spec["packaging"],
                "decision": spec["decision"],
                "proposal_rendered": str(proposalRendered).lower(),
                "purpose": spec["purpose"],
                "evidence_summary": evidenceSummary(spec),
                "coverage_summary": coverageSummary(spec),
                "proposed_steps": selectedSteps(spec, {"proposed"}),
                "manual_blocked_steps": selectedSteps(
                    spec, {"manual", "blocked"}
                ),
                "dependency_summary": dependencyReview(spec, allowCodeCopy),
                "unresolved_questions": unresolvedReview(spec),
                "update_summary": updateReview(spec),
            }
        ).rstrip()
        + "\n"
    )


def renderProvenanceReference(spec: dict[str, Any]) -> str:
    """Render a skill-local evidence and assumptions reference."""
    lines = [
        "# Forge Provenance",
        "",
        "This reference records why the generated workflow was proposed. It is not",
        "proof that every documented scenario has been observed.",
        "",
        "## Decision",
        "",
        f"- Operation: `{spec['operation']}`",
        f"- Packaging: `{spec['packaging']}`",
        f"- Coverage decision: `{spec['decision']}`",
        f"- License/copy decision: {spec['licenseDecision']}",
        "",
        "## Evidence",
        "",
    ]
    if spec["evidence"]:
        for record in spec["evidence"]:
            lines.append(
                f"- **{record['id']}** — `{record['basis']}`, "
                f"{record['confidence']} confidence; {record['source']} / "
                f"{record['locator']}: {record['summary']}"
            )
    else:
        lines.append("No evidence records.")
    lines.extend(
        [
            "",
            "## Assumptions",
            "",
            markdownList(spec["assumptions"]),
            "",
            "## Proposed steps awaiting approval",
            "",
            selectedSteps(spec, {"proposed"}),
            "",
            "## Unresolved questions",
            "",
            unresolvedReview(spec),
            "",
            "## Existing skill coverage",
            "",
            coverageSummary(spec),
            "",
        ]
    )
    return "\n".join(lines)


def renderEvaluationPrompts(spec: dict[str, Any], assetsDir: Path) -> str:
    """Render generated skill trigger and behavioral evaluations."""
    modeCase = (
        "- CBD location failure: remove or stale a sentinel and expect a location "
        "request before workflow execution."
        if spec["packaging"] == "cbd"
        else "- STD isolation: make the original source tree unavailable and expect "
        "the bundled workflow to remain usable."
    )
    template = Template(
        (assetsDir / "evaluation-prompts.template.md").read_text(encoding="utf-8")
    )
    return (
        template.substitute(
            {
                "name": spec["name"],
                "triggers": markdownList(spec["triggers"]),
                "non_triggers": markdownList(spec["nonTriggers"]),
                "mode_case": modeCase,
            }
        ).rstrip()
        + "\n"
    )


def generatedReadme(spec: dict[str, Any]) -> str:
    """Render a concise maintenance README for the generated skill."""
    modeNote = (
        "This package invokes maintained code through the local AutoCAB CBD config."
        if spec["packaging"] == "cbd"
        else "This package must contain every custom runtime helper and asset."
    )
    return (
        f"# {spec['name']}\n\n"
        f"{spec['purpose']}\n\n"
        f"Packaging: **{spec['packaging'].upper()}**. {modeNote}\n\n"
        "This package was staged by `skill-forge`. Review "
        "`references/forge-provenance.md` and resolve all pending approvals before "
        "installation.\n\n"
        "## Validation\n\n"
        "Run the package validator, each script's `--help`, unit tests, and the "
        "smallest safe end-to-end example. Runtime outputs must remain outside "
        "the skill source directory.\n"
    )


def generatedChangelog(spec: dict[str, Any]) -> str:
    """Render or prepend a generated changelog entry."""
    date = datetime.now(timezone.utc).date().isoformat()
    action = "Updated" if spec["operation"] == "update" else "Added"
    return (
        "# Changelog\n\n"
        f"## {date}\n\n"
        f"- {action} `{spec['name']}` from evidence-linked SkillSpec "
        f"(`{spec['packaging'].upper()}`, decision `{spec['decision']}`).\n"
        "- Recorded generated workflow evidence and review requirements.\n"
    )


def safeCopyExisting(source: Path, destination: Path) -> None:
    """Copy an existing skill into staging without following symlinks."""
    if not source.is_dir() or source.is_symlink():
        raise ForgeError(f"Existing skill must be a real directory: {source}")
    if not (source / "SKILL.md").is_file():
        raise ForgeError(f"Existing skill has no root SKILL.md: {source}")
    destination.mkdir(parents=True, exist_ok=False)
    for item in sorted(source.rglob("*"), key=lambda value: str(value)):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_symlink():
            raise ForgeError(f"Existing skill contains unsupported symlink: {relative}")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def prepareProposal(
    spec: dict[str, Any],
    runDir: Path,
    skillRoot: Path,
    args: argparse.Namespace,
) -> tuple[Path, list[dict[str, str]]]:
    """Create or update the staged proposal directory."""
    proposalDir = runDir / "proposal" / spec["name"]
    if spec["operation"] == "update":
        if not args.existingSkillDir:
            raise ForgeError("Update rendering requires --existingSkillDir.")
        existing = Path(args.existingSkillDir).expanduser().resolve()
        assessment = spec["updateAssessment"]
        if existing.name != assessment["targetSkill"]:
            raise ForgeError(
                "--existingSkillDir basename does not match "
                f"updateAssessment.targetSkill: {existing.name!r} != "
                f"{assessment['targetSkill']!r}"
            )
        safeCopyExisting(existing, proposalDir)
    else:
        proposalDir.mkdir(parents=True, exist_ok=False)

    assetsDir = skillRoot / "assets"
    (proposalDir / "SKILL.md").write_text(
        fillSkillTemplate(spec, assetsDir), encoding="utf-8"
    )
    if spec["operation"] == "create" or not (proposalDir / "README.md").exists():
        (proposalDir / "README.md").write_text(
            generatedReadme(spec), encoding="utf-8"
        )
    if spec["operation"] == "create" or not (proposalDir / "CHANGELOG.md").exists():
        (proposalDir / "CHANGELOG.md").write_text(
            generatedChangelog(spec), encoding="utf-8"
        )
    else:
        existingChangelog = (proposalDir / "CHANGELOG.md").read_text(encoding="utf-8")
        newEntry = generatedChangelog(spec).split("\n", 2)[2]
        (proposalDir / "CHANGELOG.md").write_text(
            "# Changelog\n\n" + newEntry.strip() + "\n\n" + existingChangelog.split("\n", 2)[-1].lstrip(),
            encoding="utf-8",
        )

    referencesDir = proposalDir / "references"
    referencesDir.mkdir(parents=True, exist_ok=True)
    (referencesDir / "forge-provenance.md").write_text(
        renderProvenanceReference(spec), encoding="utf-8"
    )
    examplesDir = proposalDir / "examples"
    examplesDir.mkdir(parents=True, exist_ok=True)
    evaluationName = (
        "forge-update-evaluation-prompts.md"
        if spec["operation"] == "update"
        else "evaluation-prompts.md"
    )
    (examplesDir / evaluationName).write_text(
        renderEvaluationPrompts(spec, assetsDir), encoding="utf-8"
    )

    scriptsDir = proposalDir / "scripts"
    copied: list[dict[str, str]] = []
    if spec["packaging"] == "cbd":
        scriptsDir.mkdir(parents=True, exist_ok=True)
        configSource = skillRoot / "scripts" / "manage_cbd_config.py"
        configTarget = scriptsDir / "cbd_config.py"
        shutil.copy2(configSource, configTarget)
        configTarget.chmod(0o755)
        copied.append(
            {
                "kind": "skill-forge-helper",
                "bundlePath": "scripts/cbd_config.py",
                "sha256": fileSha256(configTarget),
            }
        )

    if args.allowCodeCopy:
        for dependency in spec["dependencies"]:
            if dependency["kind"] != "custom_standalone":
                continue
            sourceValue = dependency["sourcePath"]
            bundleValue = dependency["bundlePath"]
            if dependency["licenseStatus"] != "approved":
                raise ForgeError(
                    f"Cannot copy {dependency['name']}: licenseStatus is not approved."
                )
            if not sourceValue or not bundleValue or not safeRelativePath(bundleValue):
                raise ForgeError(
                    f"Cannot copy {dependency['name']}: sourcePath/bundlePath invalid."
                )
            bundlePath = Path(bundleValue)
            if not bundlePath.parts or bundlePath.parts[0] not in {"scripts", "assets"}:
                raise ForgeError(
                    f"Custom bundlePath must begin with scripts/ or assets/: {bundleValue}"
                )
            source = Path(sourceValue).expanduser()
            if not source.is_file() or source.is_symlink():
                raise ForgeError(
                    f"Custom dependency is not a regular non-symlink file: {source}"
                )
            target = proposalDir / bundlePath
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(
                {
                    "kind": "custom_standalone",
                    "name": dependency["name"],
                    "bundlePath": bundlePath.as_posix(),
                    "sourceSha256": fileSha256(source),
                    "bundledSha256": fileSha256(target),
                }
            )
    return proposalDir, copied


def sanitizedDependencyRecord(dependency: dict[str, Any]) -> dict[str, Any]:
    """Remove exact local source paths from shareable dependency evidence."""
    record = dict(dependency)
    source = record.pop("sourcePath", None)
    record["sourceProvided"] = bool(source)
    record["sourceBasename"] = Path(source).name if source else None
    return record


def relativeFiles(runDir: Path) -> list[str]:
    """List all regular run artifacts relative to the run directory."""
    return [
        path.relative_to(runDir).as_posix()
        for path in sorted(runDir.rglob("*"), key=lambda value: str(value))
        if path.is_file()
    ]


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate a SkillSpec and render a deterministic staged proposal."
    )
    parser.add_argument("--spec", required=True, help="SkillSpec JSON path.")
    parser.add_argument(
        "--validateOnly",
        action="store_true",
        help="Validate and print a JSON result without writing a run directory.",
    )
    parser.add_argument(
        "--outputDir",
        help="Explicit empty run directory outside the skill-forge package.",
    )
    parser.add_argument(
        "--existingSkillDir",
        help="Existing skill source directory required for update rendering.",
    )
    parser.add_argument(
        "--allowCodeCopy",
        action="store_true",
        help="Copy approved custom_standalone files into an STD proposal.",
    )
    parser.add_argument("--runId", help="UTC run ID (YYYYMMDDTHHMMSSZ).")
    parser.add_argument(
        "--agentRequestFile", help="Caller-prepared sanitized request summary."
    )
    parser.add_argument(
        "--agentWorkflowFile", help="Caller-prepared workflow/provenance notes."
    )
    return parser.parse_args(argv)


def runForge(args: argparse.Namespace) -> int:
    """Validate and optionally render a forge run."""
    specPath = Path(args.spec).expanduser()
    try:
        spec = loadSpec(specPath)
    except SkillSpecError as exc:
        print(json.dumps({"valid": False, "issues": [str(exc)]}, indent=2))
        return 2
    issues = validateSpec(spec)
    if args.validateOnly:
        print(json.dumps({"valid": not issues, "issues": issues}, indent=2))
        return 0 if not issues else 2
    if issues:
        print(json.dumps({"valid": False, "issues": issues}, indent=2))
        return 2
    if args.allowCodeCopy and spec["packaging"] != "std":
        print(
            json.dumps(
                {
                    "valid": False,
                    "issues": ["--allowCodeCopy is valid only for STD packaging."],
                },
                indent=2,
            )
        )
        return 2
    if not args.outputDir:
        print(
            json.dumps(
                {"valid": False, "issues": ["--outputDir is required to render."]},
                indent=2,
            )
        )
        return 2

    runId = args.runId or defaultRunId()
    if not RUN_ID_PATTERN.fullmatch(runId):
        print(
            json.dumps(
                {
                    "valid": False,
                    "issues": ["--runId must use UTC YYYYMMDDTHHMMSSZ format."],
                },
                indent=2,
            )
        )
        return 2

    skillRoot = Path(__file__).resolve().parent.parent
    runDir = Path(args.outputDir).expanduser().resolve()
    ensureEmptyRunDir(runDir, skillRoot)
    logger = configureLogging(runDir)
    appendCommandLog(runDir, runId, args)
    logger.info("Starting skill-forge run %s", runId)

    copyTextInput(
        args.agentRequestFile,
        runDir / "agent_request.txt",
        "Sanitized request summary was not supplied by the calling agent.",
    )
    copyTextInput(
        args.agentWorkflowFile,
        runDir / "agent_workflow.md",
        (
            "# Agent workflow\n\n"
            "The calling agent did not supply detailed workflow notes. Review is "
            "required before installation."
        ),
    )
    writeJson(runDir / "skill-spec.json", spec)
    evidenceDir = runDir / "evidence"
    writeJson(evidenceDir / "evidence.json", spec["evidence"])
    writeJson(
        evidenceDir / "dependency-report.json",
        [sanitizedDependencyRecord(value) for value in spec["dependencies"]],
    )
    writeJson(
        evidenceDir / "coverage-report.json",
        {
            "decision": spec["decision"],
            "existingSkillCoverage": spec["existingSkillCoverage"],
        },
    )

    proposalRendered = spec["decision"] in RENDERABLE_DECISIONS
    copied: list[dict[str, str]] = []
    proposalPath: Path | None = None
    if proposalRendered:
        proposalPath, copied = prepareProposal(spec, runDir, skillRoot, args)
        logger.info("Rendered staged proposal: %s", proposalPath)
    else:
        logger.info(
            "Decision %s intentionally creates no installable proposal.",
            spec["decision"],
        )

    review = renderReview(
        spec, skillRoot / "assets", proposalRendered, args.allowCodeCopy
    )
    (runDir / "REVIEW.md").write_text(review, encoding="utf-8")
    provenance = {
        "skillForgeVersion": FORGE_VERSION,
        "skillSpecSchemaVersion": spec["schemaVersion"],
        "runId": runId,
        "generatedAtUtc": utcNow(),
        "operation": spec["operation"],
        "requestedPackaging": spec["requestedPackaging"],
        "packaging": spec["packaging"],
        "decision": spec["decision"],
        "proposalRendered": proposalRendered,
        "sourceManifests": spec.get("sourceManifests", []),
        "evidenceIds": [record["id"] for record in spec["evidence"]],
        "copiedFiles": copied,
        "humanApprovalRequired": True,
    }
    writeJson(runDir / "provenance.json", provenance)
    outputsBeforeMetadata = relativeFiles(runDir)
    metadata = {
        "skill": "skill-forge",
        "skillVersion": FORGE_VERSION,
        "runId": runId,
        "timestampUtc": utcNow(),
        "commandRedacted": (
            "python scripts/forge_skill.py --spec <SPEC> "
            "--outputDir <RUN_DIR>"
        ),
        "parameters": {
            "operation": spec["operation"],
            "packaging": spec["packaging"],
            "decision": spec["decision"],
            "allowCodeCopy": args.allowCodeCopy,
        },
        "summary": {
            "evidenceRecords": len(spec["evidence"]),
            "dependencies": len(spec["dependencies"]),
            "steps": len(spec["steps"]),
            "unresolvedQuestions": len(spec["unresolvedQuestions"]),
            "proposalRendered": proposalRendered,
        },
        "toolVersions": {
            "python": sys.version.split()[0],
            "skillForge": FORGE_VERSION,
        },
        "outputs": outputsBeforeMetadata,
        "logs": {
            "skillForge": "logs/skill-forge.log",
            "commands": "logs/commands.log",
        },
        "agentRequestFile": "agent_request.txt",
        "agentWorkflowFile": "agent_workflow.md",
    }
    writeJson(runDir / "run_metadata.json", metadata)
    logger.info("Completed skill-forge run %s", runId)
    closeLogging(logger)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the SkillSpec forge CLI."""
    args = parseArgs(argv)
    try:
        return runForge(args)
    except (ForgeError, OSError, ValueError) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(levelname)s: %(message)s")
        logging.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
