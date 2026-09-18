"""SkillSpec validation and rendering helpers for skill-forge."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
from typing import Any


SCHEMA_VERSION = "1.0"
RENDERABLE_DECISIONS = {"compose", "novel"}
OPERATIONS = {"create", "update"}
PACKAGING = {"cbd", "std"}
REQUESTED_PACKAGING = {"cbd", "std", "auto"}
DECISIONS = {"reuse", "compose", "novel", "blocked", "no-update"}
STEP_STATUSES = {"supported", "proposed", "blocked", "manual", "optional"}
EVIDENCE_BASES = {
    "observed",
    "inspected_code",
    "documented",
    "existing_skill",
    "user_confirmed",
    "domain_inference",
}
DEPENDENCY_KINDS = {
    "public_tool",
    "public_pipeline",
    "custom_standalone",
    "custom_integrated",
    "missing",
    "manual",
}
LICENSE_STATUSES = {"approved", "unknown", "prohibited", "not_applicable"}
REQUIRED_FIELDS = {
    "schemaVersion",
    "operation",
    "requestedPackaging",
    "packaging",
    "decision",
    "name",
    "title",
    "description",
    "purpose",
    "triggers",
    "nonTriggers",
    "inputs",
    "outputs",
    "steps",
    "dependencies",
    "evidence",
    "assumptions",
    "unresolvedQuestions",
    "qualityChecks",
    "failureModes",
    "existingSkillCoverage",
    "codebase",
    "updateAssessment",
    "licenseDecision",
}
OPTIONAL_FIELDS = {"sourceManifests"}


class SkillSpecError(Exception):
    """Represent invalid JSON or an invalid SkillSpec."""


def loadSpec(path: Path) -> dict[str, Any]:
    """Load a SkillSpec JSON document.

    Args:
        path (Path): JSON file to read.

    Returns:
        dict[str, Any]: Parsed object.

    Raises:
        SkillSpecError: If the file is unreadable or not a JSON object.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SkillSpecError(f"Cannot read SkillSpec {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SkillSpecError(
            f"Invalid SkillSpec JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise SkillSpecError("SkillSpec root must be a JSON object.")
    return data


def isStringList(value: Any) -> bool:
    """Return whether value is a list of non-empty strings."""
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def duplicateValues(values: list[str]) -> list[str]:
    """Return sorted duplicate strings."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def safeRelativePath(value: str) -> bool:
    """Return whether value is a safe package-relative POSIX path."""
    path = PurePosixPath(value)
    return (
        bool(value)
        and not path.is_absolute()
        and ".." not in path.parts
        and "." not in {part for part in path.parts if part}
    )


def validateEvidenceList(spec: dict[str, Any], issues: list[str]) -> set[str]:
    """Validate evidence records and return their IDs."""
    evidence = spec.get("evidence")
    if not isinstance(evidence, list):
        issues.append("evidence must be an array.")
        return set()
    ids: list[str] = []
    for index, record in enumerate(evidence):
        prefix = f"evidence[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{prefix} must be an object.")
            continue
        for field in ("id", "source", "locator", "summary"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                issues.append(f"{prefix}.{field} must be a non-empty string.")
        evidenceId = record.get("id")
        if isinstance(evidenceId, str):
            ids.append(evidenceId)
        if record.get("basis") not in EVIDENCE_BASES:
            issues.append(f"{prefix}.basis is invalid.")
        if record.get("confidence") not in {"high", "medium", "low"}:
            issues.append(f"{prefix}.confidence is invalid.")
    for duplicate in duplicateValues(ids):
        issues.append(f"Duplicate evidence ID: {duplicate}")
    return set(ids)


def validateEvidenceRefs(
    refs: Any, context: str, evidenceIds: set[str], issues: list[str]
) -> list[str]:
    """Validate an evidence ID array and return valid strings."""
    if not isStringList(refs):
        issues.append(f"{context} must be an array of non-empty evidence IDs.")
        return []
    values = list(refs)
    for duplicate in duplicateValues(values):
        issues.append(f"{context} contains duplicate ID: {duplicate}")
    for value in values:
        if value not in evidenceIds:
            issues.append(f"{context} references unknown evidence ID: {value}")
    return values


def validateIoRoles(
    spec: dict[str, Any], field: str, evidenceIds: set[str], issues: list[str]
) -> None:
    """Validate input or output role objects."""
    records = spec.get(field)
    if not isinstance(records, list):
        issues.append(f"{field} must be an array.")
        return
    for index, record in enumerate(records):
        prefix = f"{field}[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{prefix} must be an object.")
            continue
        for textField in ("name", "description"):
            if not isinstance(record.get(textField), str) or not record[
                textField
            ].strip():
                issues.append(f"{prefix}.{textField} must be a non-empty string.")
        if not isinstance(record.get("required"), bool):
            issues.append(f"{prefix}.required must be boolean.")
        refs = validateEvidenceRefs(
            record.get("evidenceIds"), f"{prefix}.evidenceIds", evidenceIds, issues
        )
        if not refs:
            issues.append(f"{prefix} needs supporting evidence.")


def validateDependencies(
    spec: dict[str, Any], evidenceIds: set[str], issues: list[str]
) -> set[str]:
    """Validate dependency records and return names."""
    dependencies = spec.get("dependencies")
    if not isinstance(dependencies, list):
        issues.append("dependencies must be an array.")
        return set()
    names: list[str] = []
    packaging = spec.get("packaging")
    renderable = spec.get("decision") in RENDERABLE_DECISIONS
    for index, dependency in enumerate(dependencies):
        prefix = f"dependencies[{index}]"
        if not isinstance(dependency, dict):
            issues.append(f"{prefix} must be an object.")
            continue
        name = dependency.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(f"{prefix}.name must be a non-empty string.")
        else:
            names.append(name)
        kind = dependency.get("kind")
        if kind not in DEPENDENCY_KINDS:
            issues.append(f"{prefix}.kind is invalid.")
        if not isinstance(dependency.get("required"), bool):
            issues.append(f"{prefix}.required must be boolean.")
        validateEvidenceRefs(
            dependency.get("evidenceIds"),
            f"{prefix}.evidenceIds",
            evidenceIds,
            issues,
        )
        if dependency.get("licenseStatus") not in LICENSE_STATUSES:
            issues.append(f"{prefix}.licenseStatus is invalid.")
        for nullableField in (
            "install",
            "versionConstraint",
            "sourcePath",
            "bundlePath",
        ):
            value = dependency.get(nullableField)
            if value is not None and not isinstance(value, str):
                issues.append(f"{prefix}.{nullableField} must be string or null.")
        if not isinstance(dependency.get("notes"), str):
            issues.append(f"{prefix}.notes must be a string.")

        if packaging == "std" and renderable:
            if kind in {"custom_integrated", "missing"} and dependency.get("required"):
                issues.append(
                    f"{prefix}: required {kind} dependency is incompatible with final STD packaging."
                )
            if kind == "custom_standalone" and dependency.get("required"):
                sourcePath = dependency.get("sourcePath")
                bundlePath = dependency.get("bundlePath")
                if not isinstance(sourcePath, str) or not sourcePath:
                    issues.append(f"{prefix}: STD custom sourcePath is required.")
                if not isinstance(bundlePath, str) or not safeRelativePath(bundlePath):
                    issues.append(
                        f"{prefix}: STD bundlePath must be a safe relative path."
                    )
                if dependency.get("licenseStatus") != "approved":
                    issues.append(
                        f"{prefix}: STD custom code requires licenseStatus 'approved'."
                    )
        if kind in {"public_tool", "public_pipeline"} and dependency.get(
            "sourcePath"
        ):
            issues.append(f"{prefix}: public dependencies must not be copied.")
    for duplicate in duplicateValues(names):
        issues.append(f"Duplicate dependency name: {duplicate}")
    return set(names)


def validateSteps(
    spec: dict[str, Any],
    evidenceIds: set[str],
    dependencyNames: set[str],
    issues: list[str],
) -> None:
    """Validate ordered workflow steps."""
    steps = spec.get("steps")
    if not isinstance(steps, list):
        issues.append("steps must be an array.")
        return
    ids: list[str] = []
    hasBlocked = False
    for index, step in enumerate(steps):
        prefix = f"steps[{index}]"
        if not isinstance(step, dict):
            issues.append(f"{prefix} must be an object.")
            continue
        stepId = step.get("id")
        if not isinstance(stepId, str) or not stepId.strip():
            issues.append(f"{prefix}.id must be a non-empty string.")
        else:
            ids.append(stepId)
        if not isinstance(step.get("summary"), str) or not step["summary"].strip():
            issues.append(f"{prefix}.summary must be a non-empty string.")
        status = step.get("status")
        basis = step.get("basis")
        if status not in STEP_STATUSES:
            issues.append(f"{prefix}.status is invalid.")
        if basis not in EVIDENCE_BASES:
            issues.append(f"{prefix}.basis is invalid.")
        refs = validateEvidenceRefs(
            step.get("evidenceIds"),
            f"{prefix}.evidenceIds",
            evidenceIds,
            issues,
        )
        existingSkill = step.get("existingSkill")
        if existingSkill is not None and (
            not isinstance(existingSkill, str) or not existingSkill.strip()
        ):
            issues.append(f"{prefix}.existingSkill must be string or null.")
        commandShape = step.get("commandShape")
        if commandShape is not None and not isinstance(commandShape, str):
            issues.append(f"{prefix}.commandShape must be string or null.")
        usedDependencies = step.get("dependencies")
        if not isStringList(usedDependencies):
            if usedDependencies != []:
                issues.append(f"{prefix}.dependencies must be a string array.")
            usedDependencies = []
        for name in usedDependencies:
            if name not in dependencyNames:
                issues.append(f"{prefix} references unknown dependency: {name}")
        if not isinstance(step.get("rationale"), str):
            issues.append(f"{prefix}.rationale must be a string.")
        if not isinstance(step.get("approvalRequired"), bool):
            issues.append(f"{prefix}.approvalRequired must be boolean.")

        if status in {"supported", "optional", "manual"} and not (
            refs or existingSkill
        ):
            issues.append(
                f"{prefix}: {status} step needs evidence or an existing skill source."
            )
        if status == "proposed":
            if basis != "domain_inference":
                issues.append(f"{prefix}: proposed step basis must be domain_inference.")
            if step.get("approvalRequired") is not True:
                issues.append(f"{prefix}: proposed step requires approvalRequired true.")
            if not step.get("rationale", "").strip():
                issues.append(f"{prefix}: proposed step requires a rationale.")
        elif step.get("approvalRequired") and status != "manual":
            issues.append(
                f"{prefix}: approvalRequired is reserved for proposed or manual steps."
            )
        if status == "blocked":
            hasBlocked = True

    for duplicate in duplicateValues(ids):
        issues.append(f"Duplicate step ID: {duplicate}")

    blockingQuestions = [
        question
        for question in spec.get("unresolvedQuestions", [])
        if isinstance(question, dict) and question.get("blocking") is True
    ]
    if hasBlocked and not blockingQuestions:
        issues.append("Blocked workflow steps require a blocking unresolved question.")


def validateQuestions(spec: dict[str, Any], issues: list[str]) -> None:
    """Validate unresolved questions."""
    questions = spec.get("unresolvedQuestions")
    if not isinstance(questions, list):
        issues.append("unresolvedQuestions must be an array.")
        return
    ids: list[str] = []
    for index, question in enumerate(questions):
        prefix = f"unresolvedQuestions[{index}]"
        if not isinstance(question, dict):
            issues.append(f"{prefix} must be an object.")
            continue
        for field in ("id", "question"):
            if not isinstance(question.get(field), str) or not question[field].strip():
                issues.append(f"{prefix}.{field} must be a non-empty string.")
        if isinstance(question.get("id"), str):
            ids.append(question["id"])
        if not isinstance(question.get("blocking"), bool):
            issues.append(f"{prefix}.blocking must be boolean.")
        if not isStringList(question.get("neededArtifacts")):
            if question.get("neededArtifacts") != []:
                issues.append(f"{prefix}.neededArtifacts must be a string array.")
    for duplicate in duplicateValues(ids):
        issues.append(f"Duplicate unresolved question ID: {duplicate}")


def validateCodebase(spec: dict[str, Any], issues: list[str]) -> None:
    """Validate CBD roots/sentinels and STD absence of roots."""
    codebase = spec.get("codebase")
    if not isinstance(codebase, dict) or not isinstance(codebase.get("roots"), list):
        issues.append("codebase must be an object with a roots array.")
        return
    roots = codebase["roots"]
    packaging = spec.get("packaging")
    renderable = spec.get("decision") in RENDERABLE_DECISIONS
    blocking = any(
        isinstance(question, dict) and question.get("blocking") is True
        for question in spec.get("unresolvedQuestions", [])
    )
    if packaging == "std" and roots:
        issues.append("STD SkillSpec codebase.roots must be empty.")
    if packaging == "cbd" and renderable and not roots and not blocking:
        issues.append("Renderable CBD SkillSpec needs a root or a blocking question.")
    for index, root in enumerate(roots):
        prefix = f"codebase.roots[{index}]"
        if not isinstance(root, dict):
            issues.append(f"{prefix} must be an object.")
            continue
        if not isinstance(root.get("path"), str) or not root["path"].strip():
            issues.append(f"{prefix}.path must be a non-empty string.")
        sentinels = root.get("sentinels")
        if not isStringList(sentinels) or not sentinels:
            issues.append(f"{prefix}.sentinels must be a non-empty string array.")
        else:
            for sentinel in sentinels:
                if not safeRelativePath(sentinel):
                    issues.append(f"{prefix} has unsafe sentinel: {sentinel}")
        remote = root.get("gitRemote")
        if remote is not None and not isinstance(remote, str):
            issues.append(f"{prefix}.gitRemote must be string or null.")


def validateUpdate(spec: dict[str, Any], issues: list[str]) -> None:
    """Validate update/no-update and migration invariants."""
    operation = spec.get("operation")
    assessment = spec.get("updateAssessment")
    if operation == "create":
        if assessment is not None:
            issues.append("Create operation requires updateAssessment null.")
        return
    if not isinstance(assessment, dict):
        issues.append("Update operation requires updateAssessment object.")
        return
    if not isinstance(assessment.get("targetSkill"), str) or not assessment[
        "targetSkill"
    ].strip():
        issues.append("updateAssessment.targetSkill must be non-empty.")
    if not isinstance(assessment.get("materialDelta"), bool):
        issues.append("updateAssessment.materialDelta must be boolean.")
    if not isinstance(assessment.get("summary"), str) or not assessment[
        "summary"
    ].strip():
        issues.append("updateAssessment.summary must be non-empty.")
    migration = assessment.get("migrationSuggested")
    if migration not in {"none", "cbd-to-std", "std-to-cbd"}:
        issues.append("updateAssessment.migrationSuggested is invalid.")
    if not isinstance(assessment.get("migrationApproved"), bool):
        issues.append("updateAssessment.migrationApproved must be boolean.")
    if migration != "none" and not assessment.get("migrationApproved"):
        if spec.get("decision") in RENDERABLE_DECISIONS:
            issues.append("Renderable packaging migration requires explicit approval.")
    if spec.get("decision") == "no-update" and assessment.get("materialDelta") is not False:
        issues.append("no-update decision requires materialDelta false.")
    if (
        spec.get("decision") in RENDERABLE_DECISIONS
        and assessment.get("materialDelta") is False
    ):
        issues.append("Renderable update requires a material delta.")


def validateCoverage(spec: dict[str, Any], issues: list[str]) -> None:
    """Validate existing skill coverage records."""
    coverage = spec.get("existingSkillCoverage")
    if not isinstance(coverage, list):
        issues.append("existingSkillCoverage must be an array.")
        return
    for index, record in enumerate(coverage):
        prefix = f"existingSkillCoverage[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{prefix} must be an object.")
            continue
        if not isinstance(record.get("skill"), str) or not record["skill"].strip():
            issues.append(f"{prefix}.skill must be a non-empty string.")
        version = record.get("version")
        if version is not None and not isinstance(version, str):
            issues.append(f"{prefix}.version must be string or null.")
        for field in ("actionsCovered", "gaps"):
            if not isStringList(record.get(field)) and record.get(field) != []:
                issues.append(f"{prefix}.{field} must be a string array.")
        if not isinstance(record.get("handoffCompatible"), bool):
            issues.append(f"{prefix}.handoffCompatible must be boolean.")
    if spec.get("decision") == "reuse" and not coverage:
        issues.append("reuse decision requires existingSkillCoverage evidence.")


def validateFailureModes(spec: dict[str, Any], issues: list[str]) -> None:
    """Validate quality checks, assumptions, and failure modes."""
    for field in ("triggers", "nonTriggers", "assumptions", "qualityChecks"):
        value = spec.get(field)
        if not isStringList(value) and value != []:
            issues.append(f"{field} must be an array of non-empty strings.")
    failures = spec.get("failureModes")
    if not isinstance(failures, list):
        issues.append("failureModes must be an array.")
        return
    for index, failure in enumerate(failures):
        if not isinstance(failure, dict):
            issues.append(f"failureModes[{index}] must be an object.")
            continue
        for field in ("trigger", "response"):
            if not isinstance(failure.get(field), str) or not failure[field].strip():
                issues.append(
                    f"failureModes[{index}].{field} must be a non-empty string."
                )


def validateSpec(spec: dict[str, Any]) -> list[str]:
    """Return all detected SkillSpec invariant violations."""
    issues: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(spec))
    unknown = sorted(set(spec) - REQUIRED_FIELDS - OPTIONAL_FIELDS)
    issues.extend(f"Missing required field: {field}" for field in missing)
    issues.extend(f"Unknown top-level field: {field}" for field in unknown)
    if missing:
        return issues

    if spec.get("schemaVersion") != SCHEMA_VERSION:
        issues.append(f"schemaVersion must be {SCHEMA_VERSION!r}.")
    if spec.get("operation") not in OPERATIONS:
        issues.append("operation must be create or update.")
    if spec.get("requestedPackaging") not in REQUESTED_PACKAGING:
        issues.append("requestedPackaging must be cbd, std, or auto.")
    if spec.get("packaging") not in PACKAGING:
        issues.append("packaging must be cbd or std.")
    if spec.get("decision") not in DECISIONS:
        issues.append("decision is invalid.")

    name = spec.get("name")
    if not isinstance(name, str) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", name
    ):
        issues.append("name must contain lowercase letters/numbers and single hyphens.")
    elif len(name) > 64:
        issues.append("name must be at most 64 characters.")
    elif spec.get("operation") == "create":
        expectedSuffix = f"-{spec.get('packaging')}"
        if not name.endswith(expectedSuffix):
            issues.append(f"Create name must end in {expectedSuffix}.")

    for field in ("title", "description", "purpose", "licenseDecision"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            issues.append(f"{field} must be a non-empty string.")
    if isinstance(spec.get("description"), str) and len(spec["description"]) > 1024:
        issues.append("description must be at most 1024 characters.")

    evidenceIds = validateEvidenceList(spec, issues)
    validateIoRoles(spec, "inputs", evidenceIds, issues)
    validateIoRoles(spec, "outputs", evidenceIds, issues)
    validateQuestions(spec, issues)
    dependencyNames = validateDependencies(spec, evidenceIds, issues)
    validateSteps(spec, evidenceIds, dependencyNames, issues)
    validateCodebase(spec, issues)
    validateCoverage(spec, issues)
    validateUpdate(spec, issues)
    validateFailureModes(spec, issues)

    if spec.get("decision") == "blocked" and not any(
        isinstance(question, dict) and question.get("blocking") is True
        for question in spec.get("unresolvedQuestions", [])
    ):
        issues.append("blocked decision requires a blocking unresolved question.")
    if spec.get("decision") in RENDERABLE_DECISIONS and any(
        isinstance(question, dict) and question.get("blocking") is True
        for question in spec.get("unresolvedQuestions", [])
    ):
        issues.append(
            "Renderable decisions cannot retain blocking unresolved questions; "
            "use decision 'blocked'."
        )
    if spec.get("decision") == "no-update" and spec.get("operation") != "update":
        issues.append("no-update decision requires update operation.")

    sourceManifests = spec.get("sourceManifests", [])
    if not isStringList(sourceManifests) and sourceManifests != []:
        issues.append("sourceManifests must be a string array.")
    return sorted(set(issues))


def markdownList(values: list[str], emptyText: str = "None documented.") -> str:
    """Render strings as a Markdown list."""
    if not values:
        return emptyText
    return "\n".join(f"- {value}" for value in values)


def jsonScalar(value: str) -> str:
    """Render a string as a JSON/YAML-compatible quoted scalar."""
    return json.dumps(value, ensure_ascii=False)
