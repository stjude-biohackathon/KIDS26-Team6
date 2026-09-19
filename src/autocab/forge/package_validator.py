#!/usr/bin/env python3
"""Validate structural and CBD/STD invariants of an AutoCAB skill package."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import unquote


NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PRIVATE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])/(?:home|Users|research_jude)/[^\s`\"')]+"),
    re.compile(r"(?<![A-Za-z0-9])~/(?:[^\s`\"')]+)"),
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s`\"')]+"),
)
REQUIRED_RUN_ARTIFACTS = {
    "agent_request.txt",
    "agent_workflow.md",
    "commands.sh",
    "logs/commands.log",
    "logs/commands.jsonl",
    "logs/parameters.jsonl",
    "run_manifest.json",
    "run_manifest.md",
    "run_summary.json",
    "run_summary.md",
}


def hasVersionConstraint(value: str) -> bool:
    """Return whether a package declaration carries a version constraint."""
    return bool(re.search(r"(?:[<>=!~]=?|@\s*\S|@sha256:)", value))


def normalizedPackageName(value: str) -> str:
    """Extract a comparison-safe package name."""
    candidate = value.split("::")[-1].strip()
    candidate = re.split(
        r"\s+@\s+|(?<=[A-Za-z0-9_./-])@(?=[vV0-9])|[<>=!~\s]",
        candidate,
        maxsplit=1,
    )[0]
    candidate = candidate.split("[", 1)[0]
    return re.sub(r"[-_.]+", "-", candidate).lower()


def canonicalConstraintExpression(value: str) -> str:
    """Canonicalize a dependency-side exact or range constraint."""
    candidate = value.replace(" ", "")
    if candidate.startswith("@"):
        candidate = candidate[1:]
    exact = re.fullmatch(r"={1,2}(.+)", candidate)
    if exact:
        return "exact:" + exact.group(1).lstrip("vV")
    if re.fullmatch(r"[vV]?[0-9][0-9A-Za-z.+_-]*", candidate):
        return "exact:" + candidate.lstrip("vV")
    return "range:" + candidate


def declaredConstraint(value: str) -> str | None:
    """Extract and canonicalize the constraint from a package declaration."""
    candidate = value.split("::")[-1].strip()
    direct = re.match(r"^[A-Za-z0-9_.\-/]+(?:\[[^]]+\])?\s+@\s+(.+)$", candidate)
    if direct:
        return "direct:" + direct.group(1).strip()
    match = re.match(r"^[A-Za-z0-9_.\-/]+(?:\[[^]]+\])?(.*)$", candidate)
    if not match or not match.group(1).strip():
        return None
    return canonicalConstraintExpression(match.group(1).strip())


def constraintsCompatible(expected: str, declared: str | None) -> bool:
    """Return whether a mapped declaration preserves an expected constraint."""
    if declared is None:
        return False
    if expected.startswith("exact:") and declared.startswith("exact:"):
        expectedValue = expected.removeprefix("exact:")
        declaredValue = declared.removeprefix("exact:")
        if "=" not in expectedValue:
            return declaredValue == expectedValue or declaredValue.startswith(expectedValue + "=")
    return declared == expected


def parseFrontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse basic top-level YAML scalars from SKILL.md frontmatter.

    This intentionally supports only the fields needed for structural checks;
    it is not a general YAML parser.
    """
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter delimiter.")
    closing = text.find("\n---", 4)
    if closing < 0:
        raise ValueError("SKILL.md frontmatter has no closing delimiter.")
    frontmatter = text[4:closing]
    body = text[closing + 4 :].lstrip("\n")
    values: dict[str, str] = {}
    lines = frontmatter.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s*(.*))?$", line)
        if not match:
            index += 1
            continue
        key, raw = match.group(1), (match.group(2) or "").strip()
        if raw in {">", ">-", "|", "|-"}:
            folded: list[str] = []
            index += 1
            while index < len(lines) and (
                not lines[index].strip() or lines[index].startswith((" ", "\t"))
            ):
                if lines[index].strip():
                    folded.append(lines[index].strip())
                index += 1
            values[key] = " ".join(folded)
            continue
        if raw.startswith('"') and raw.endswith('"'):
            try:
                values[key] = json.loads(raw)
            except json.JSONDecodeError:
                values[key] = raw.strip('"')
        elif raw.startswith("'") and raw.endswith("'"):
            values[key] = raw[1:-1].replace("''", "'")
        else:
            values[key] = raw
        index += 1
    for metadataField in ("version", "packaging", "generated_by", "status"):
        metadataMatch = re.search(
            rf"(?m)^  {re.escape(metadataField)}:\s*(.+?)\s*$",
            frontmatter,
        )
        if not metadataMatch:
            continue
        rawValue = metadataMatch.group(1)
        if rawValue.startswith('"') and rawValue.endswith('"'):
            try:
                parsedValue = json.loads(rawValue)
            except json.JSONDecodeError:
                parsedValue = rawValue.strip('"')
        elif rawValue.startswith("'") and rawValue.endswith("'"):
            parsedValue = rawValue[1:-1].replace("''", "'")
        else:
            parsedValue = rawValue
        values[f"metadata.{metadataField}"] = parsedValue
    return values, body


def linkTarget(raw: str) -> str:
    """Extract the path component from a Markdown link target."""
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    if " " in value:
        value = value.split(" ", 1)[0]
    return unquote(value)


def isInside(path: Path, root: Path) -> bool:
    """Return whether a resolved path remains inside root."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def inspectLinks(root: Path, errors: list[str]) -> None:
    """Verify local Markdown links stay inside the package and exist."""
    for markdown in sorted(root.rglob("*.md"), key=lambda path: str(path)):
        if markdown.is_symlink() or markdown.name.endswith(".template.md"):
            continue
        try:
            text = markdown.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"Cannot read Markdown file {markdown.relative_to(root)}: {exc}")
            continue
        for raw in MARKDOWN_LINK_PATTERN.findall(text):
            targetValue = linkTarget(raw)
            if not targetValue or targetValue.startswith(("#", "http://", "https://", "mailto:")):
                continue
            targetValue = targetValue.split("#", 1)[0]
            target = markdown.parent / targetValue
            relativeSource = markdown.relative_to(root)
            if not isInside(target, root):
                errors.append(f"{relativeSource}: link escapes skill package: {targetValue}")
            elif not target.exists():
                errors.append(f"{relativeSource}: linked resource does not exist: {targetValue}")


def inspectSymlinks(root: Path, errors: list[str]) -> None:
    """Reject symlinks inside a portable generated skill package."""
    for path in sorted(root.rglob("*"), key=lambda value: str(value)):
        if path.is_symlink():
            errors.append(f"Skill package contains symlink: {path.relative_to(root)}")


def inspectTemplateTokens(root: Path, errors: list[str]) -> None:
    """Detect unrendered template syntax in text files."""
    textSuffixes = {".md", ".json", ".sh", ".txt", ".yaml", ".yml"}
    for path in sorted(root.rglob("*"), key=lambda value: str(value)):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in textSuffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r"\{\{[^}]+\}\}", text):
            errors.append(f"Unrendered '{{{{...}}}}' token in {path.relative_to(root)}")
        if path.name.endswith(".template.md"):
            continue
        token = re.search(r"(?m)^\s*\$(?:name|title|purpose|workflow|dependencies)\s*$", text)
        if token:
            errors.append(f"Unrendered template token in {path.relative_to(root)}")


def inspectPrivatePaths(root: Path, errors: list[str]) -> None:
    """Detect common machine-specific absolute paths in an STD package."""
    textSuffixes = {
        ".bash",
        ".cfg",
        ".conf",
        ".json",
        ".md",
        ".nf",
        ".py",
        ".r",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
    for path in sorted(root.rglob("*"), key=lambda value: str(value)):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in textSuffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in PRIVATE_PATH_PATTERNS:
            match = pattern.search(text)
            if match:
                errors.append(
                    f"STD package contains machine-specific path in "
                    f"{path.relative_to(root)}: {match.group(0)}"
                )
                break


def readJsonObject(path: Path, errors: list[str]) -> dict[str, Any] | None:
    """Read a JSON object and append an actionable validation error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        errors.append(f"Cannot read {path.name}: {exc}")
        return None
    except json.JSONDecodeError as exc:
        errors.append(f"Invalid {path.name} at line {exc.lineno}: {exc.msg}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{path.name} must contain a JSON object.")
        return None
    return value


def validateRuntimeContract(
    root: Path,
    name: str,
    skillVersion: str,
    packaging: str,
    body: str,
    allowDraft: bool,
    errors: list[str],
    warnings: list[str],
) -> None:
    """Validate environment and per-execution provenance artifacts."""
    for relative in (
        "scripts/record_run.py",
        "references/runtime-reproducibility.md",
        "examples/run-summary.template.json",
    ):
        if not (root / relative).is_file():
            errors.append(f"Generated package is missing {relative}.")
    runtimeReferencePath = root / "references" / "runtime-reproducibility.md"
    if runtimeReferencePath.is_file():
        runtimeReference = runtimeReferencePath.read_text(encoding="utf-8")
        for token in (
            "--category",
            "--parameter",
            "--consumes",
            "--produces",
            "record-parameter",
            "record-skill",
            "record-version",
            "environmentSnapshot",
        ):
            if token not in runtimeReference:
                errors.append("Runtime reproducibility reference omits {!r}.".format(token))
    summaryTemplatePath = root / "examples" / "run-summary.template.json"
    if summaryTemplatePath.is_file():
        summaryTemplate = readJsonObject(summaryTemplatePath, errors)
        requiredSummaryFields = {
            "status",
            "whatWasDone",
            "findings",
            "parameters",
            "inputs",
            "outputs",
            "environmentSnapshot",
            "skillsUsed",
            "versions",
            "warnings",
            "assumptions",
            "manualSteps",
            "limitations",
        }
        if summaryTemplate is not None:
            missingSummaryFields = sorted(requiredSummaryFields - set(summaryTemplate))
            if missingSummaryFields:
                errors.append(
                    "Run summary template omits fields: " + ", ".join(missingSummaryFields)
                )

    requiredInstructions = (
        "record_run.py init",
        "record_run.py exec",
        "agent_request.txt",
        "commands.sh",
        "logs/commands.log",
        "logs/commands.jsonl",
        "logs/parameters.jsonl",
        "run_manifest.json",
        "run_manifest.md",
        "run_summary.json",
        "run_summary.md",
        "resolved environment snapshot",
    )
    for instruction in requiredInstructions:
        if instruction.lower() not in body.lower():
            errors.append(f"SKILL.md runtime contract does not mention {instruction!r}.")
    if "display" not in body.lower() or "chat" not in body.lower():
        errors.append("SKILL.md must require displaying the finalized findings summary in chat.")

    packagePath = root / "skill-package.json"
    if not packagePath.is_file():
        errors.append("Generated package is missing skill-package.json.")
        return
    package = readJsonObject(packagePath, errors)
    if package is None:
        return
    if package.get("schemaVersion") != "1.0":
        errors.append("skill-package.json schemaVersion must be '1.0'.")
    if package.get("name") != name:
        errors.append("skill-package.json name does not match SKILL.md.")
    if package.get("version") != skillVersion:
        errors.append("skill-package.json version does not match SKILL.md metadata.")
    if package.get("packaging") != packaging:
        errors.append("skill-package.json packaging does not match the skill mode.")
    if package.get("generatedBy") != "skill-forge":
        errors.append("skill-package.json generatedBy must be 'skill-forge'.")
    if not isinstance(package.get("commandExecutionExpected"), bool):
        errors.append("skill-package.json commandExecutionExpected must be boolean.")

    declaredArtifacts = package.get("requiredRunArtifacts")
    if not isinstance(declaredArtifacts, list) or not all(
        isinstance(value, str) for value in declaredArtifacts
    ):
        errors.append("skill-package.json requiredRunArtifacts must be a string array.")
    else:
        missingArtifacts = sorted(REQUIRED_RUN_ARTIFACTS - set(declaredArtifacts))
        if missingArtifacts:
            errors.append(
                "skill-package.json omits required run artifacts: " + ", ".join(missingArtifacts)
            )

    environment = package.get("runtimeEnvironment")
    if not isinstance(environment, dict):
        errors.append("skill-package.json runtimeEnvironment must be an object.")
        return
    manager = environment.get("manager")
    primaryByManager = {
        "conda": "environment.yml",
        "venv": "requirements.txt",
        "container": "container-image.txt",
        "system": "system-requirements.txt",
        "codebase": None,
        "none": None,
    }
    if manager not in primaryByManager:
        errors.append("skill-package.json runtime environment manager is invalid.")
        return
    primary = primaryByManager[manager]
    if primary is not None:
        primaryPath = root / primary
        if not primaryPath.is_file() or not primaryPath.read_text(encoding="utf-8").strip():
            errors.append(f"Runtime manager {manager!r} requires non-empty {primary}.")
    if manager == "conda" and (root / "environment.yml").is_file():
        environmentText = (root / "environment.yml").read_text(encoding="utf-8")
        for field in ("channels", "condaDependencies", "pipDependencies"):
            values = environment.get(field)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str) and json.dumps(value) not in environmentText:
                        errors.append(
                            f"environment.yml omits runtimeEnvironment {field} value {value!r}."
                        )
    if manager == "venv" and (root / "requirements.txt").is_file():
        requirementLines = [
            value.strip()
            for value in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if value.strip()
        ]
        if requirementLines != environment.get("pipDependencies"):
            errors.append("requirements.txt does not match runtimeEnvironment pipDependencies.")
    externalArtifacts = environment.get("externalArtifacts")
    if not isinstance(externalArtifacts, list):
        errors.append("runtimeEnvironment.externalArtifacts must be an array.")
        externalArtifacts = []
    elif externalArtifacts:
        externalPath = root / "external-artifacts.txt"
        if not externalPath.is_file() or not externalPath.read_text(encoding="utf-8").strip():
            errors.append("Runtime externalArtifacts require non-empty external-artifacts.txt.")
        else:
            externalLines = [
                value.strip()
                for value in externalPath.read_text(encoding="utf-8").splitlines()
                if value.strip()
            ]
            if externalLines != externalArtifacts:
                errors.append("external-artifacts.txt does not match runtimeEnvironment.")
    pythonConstraint = environment.get("python")
    if not isinstance(pythonConstraint, str) or not pythonConstraint.strip():
        errors.append("runtimeEnvironment must declare Python for scripts/record_run.py.")
    if (
        manager in {"venv", "none", "system", "codebase"}
        and not (root / "python-requirement.txt").is_file()
    ):
        errors.append(f"Runtime manager {manager!r} requires python-requirement.txt.")
    elif manager in {"venv", "none", "system", "codebase"}:
        pythonFileValue = (root / "python-requirement.txt").read_text(encoding="utf-8").strip()
        if pythonFileValue != pythonConstraint:
            errors.append("python-requirement.txt does not match runtimeEnvironment.")
    if manager == "codebase":
        if packaging != "cbd":
            errors.append("Codebase runtime environment is valid only for CBD.")
        codebaseFile = environment.get("codebaseEnvironmentFile")
        if not isinstance(codebaseFile, str) or not codebaseFile.strip():
            errors.append("Codebase runtime requires codebaseEnvironmentFile in package metadata.")
    if manager == "container":
        image = environment.get("containerImage")
        if environment.get("lockStrategy") == "container-digest" and (
            not isinstance(image, str) or "@sha256:" not in image
        ):
            errors.append("Container lock strategy requires an immutable image digest.")
        imagePath = root / "container-image.txt"
        if imagePath.is_file() and imagePath.read_text(encoding="utf-8").strip() != image:
            errors.append("container-image.txt does not match runtimeEnvironment.")
    if environment.get("systemDependencies"):
        systemPath = root / "system-requirements.txt"
        if not systemPath.is_file():
            errors.append("systemDependencies require system-requirements.txt.")
        else:
            systemLines = [
                value.strip()
                for value in systemPath.read_text(encoding="utf-8").splitlines()
                if value.strip()
            ]
            if systemLines != environment.get("systemDependencies"):
                errors.append("system-requirements.txt does not match runtimeEnvironment.")
    if manager in {"conda", "venv", "system"}:
        packageFields = (
            ("condaDependencies", "pipDependencies")
            if manager == "conda"
            else (("pipDependencies",) if manager == "venv" else ("systemDependencies",))
        )
        packageMap: dict[str, list[str]] = {}
        for field in packageFields:
            values = environment.get(field)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str):
                        packageMap.setdefault(normalizedPackageName(value), []).append(value)
        artifactMap: dict[str, list[str]] = {}
        for value in externalArtifacts:
            if isinstance(value, str):
                artifactMap.setdefault(normalizedPackageName(value), []).append(value)
        dependencies = package.get("dependencies")
        if not isinstance(dependencies, list):
            errors.append("skill-package.json dependencies must be an array.")
        else:
            for index, dependency in enumerate(dependencies):
                if (
                    not isinstance(dependency, dict)
                    or dependency.get("kind") not in {"public_tool", "public_pipeline"}
                    or dependency.get("required") is not True
                ):
                    continue
                if dependency.get("kind") == "public_pipeline" and not (
                    isinstance(dependency.get("versionConstraint"), str)
                    and dependency["versionConstraint"].strip()
                ):
                    errors.append(
                        "Required public pipeline at index {} has no version constraint.".format(
                            index
                        )
                    )
                environmentPackage = dependency.get("environmentPackage")
                if not isinstance(environmentPackage, str):
                    errors.append(
                        "Required public dependency at index {} is not mapped to "
                        "the environment specification.".format(index)
                    )
                    continue
                availableMap = (
                    artifactMap if dependency.get("kind") == "public_pipeline" else packageMap
                )
                matchingSpecs = availableMap.get(normalizedPackageName(environmentPackage), [])
                if not matchingSpecs:
                    errors.append(
                        "Required public dependency at index {} is not mapped to "
                        "the environment specification.".format(index)
                    )
                    continue
                versionConstraint = dependency.get("versionConstraint")
                if (
                    isinstance(versionConstraint, str)
                    and versionConstraint.strip()
                    and not any(
                        constraintsCompatible(
                            canonicalConstraintExpression(versionConstraint),
                            declaredConstraint(value),
                        )
                        for value in matchingSpecs
                    )
                ):
                    errors.append(
                        "Required public dependency at index {} has a version "
                        "constraint absent from its environment declaration.".format(index)
                    )
    if environment.get("lockStrategy") == "direct-pins":
        packageValues: list[str] = []
        for field in (
            "condaDependencies",
            "pipDependencies",
            "systemDependencies",
            "externalArtifacts",
        ):
            values = environment.get(field)
            if not isinstance(values, list):
                errors.append(f"runtimeEnvironment.{field} must be an array.")
                continue
            packageValues.extend(value for value in values if isinstance(value, str))
        unpinned = [value for value in packageValues if not hasVersionConstraint(value)]
        if unpinned:
            errors.append("Runtime direct-pins lacks constraints for: " + ", ".join(unpinned))
        if isinstance(pythonConstraint, str) and not hasVersionConstraint(pythonConstraint):
            errors.append("Runtime direct-pins requires a constrained Python version.")

    workflowStatuses = set(re.findall(r"\(`(supported|proposed|manual|blocked)`, basis:", body))
    manualOnly = (
        package.get("commandExecutionExpected") is False
        and workflowStatuses == {"manual"}
        and package.get("dependencies") == []
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
    verified = environment.get("verified")
    unresolved = environment.get("lockStrategy") == "unresolved"
    if verified is not True or unresolved:
        message = (
            "Runtime environment is not clean-environment verified or retains an "
            "unresolved lock strategy."
        )
        if manualOnly and not unresolved:
            warnings.append(
                "Manual-only package does not claim executable runtime reproducibility."
            )
        elif allowDraft:
            warnings.append(message)
        else:
            errors.append(message)


def validatePackage(
    root: Path,
    expectedPackaging: str | None,
    strict: bool,
    allowProposed: bool,
) -> dict[str, Any]:
    """Validate one Agent Skill package and return a structured report."""
    errors: list[str] = []
    warnings: list[str] = []
    if not root.exists() or not root.is_dir():
        return {
            "valid": False,
            "errors": [f"Skill directory does not exist: {root}"],
            "warnings": [],
        }
    if root.is_symlink():
        errors.append("Validate the source directory, not an installation symlink.")

    skillFiles = [
        path for path in root.rglob("SKILL.md") if path.is_file() and not path.is_symlink()
    ]
    if len(skillFiles) != 1:
        errors.append(f"Expected exactly one SKILL.md, found {len(skillFiles)}.")
    skillPath = root / "SKILL.md"
    if not skillPath.is_file():
        errors.append("Missing root SKILL.md.")
        return {"valid": False, "errors": errors, "warnings": warnings}

    text = skillPath.read_text(encoding="utf-8")
    lineCount = len(text.splitlines())
    if lineCount > 500:
        errors.append(f"SKILL.md has {lineCount} lines; maximum is 500.")
    try:
        frontmatter, body = parseFrontmatter(text)
    except ValueError as exc:
        errors.append(str(exc))
        frontmatter, body = {}, ""

    name = frontmatter.get("name", "")
    skillVersion = frontmatter.get("metadata.version", "")
    description = frontmatter.get("description", "")
    if not name:
        errors.append("Frontmatter name is required.")
    elif not NAME_PATTERN.fullmatch(name):
        errors.append("Frontmatter name must use lowercase letters/numbers and hyphens.")
    elif len(name) > 64:
        errors.append("Frontmatter name exceeds 64 characters.")
    if name and name != root.name:
        errors.append(f"Directory/name mismatch: {root.name!r} != {name!r}.")
    if not description:
        errors.append("Frontmatter description is required.")
    elif len(description) > 1024:
        errors.append("Frontmatter description exceeds 1024 characters.")
    elif "when" not in description.lower():
        warnings.append("Description may not explain when the skill should trigger.")
    if not skillVersion:
        errors.append("Frontmatter metadata.version is required.")
    elif not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?",
        skillVersion,
    ):
        errors.append("Frontmatter metadata.version must be a semantic version.")

    if not (root / "CHANGELOG.md").is_file():
        errors.append("Missing CHANGELOG.md.")
    if not (root / "README.md").is_file():
        warnings.append("Complex skills should include README.md.")

    detectedPackaging = expectedPackaging
    if detectedPackaging is None:
        if name.endswith("-cbd"):
            detectedPackaging = "cbd"
        elif name.endswith("-std"):
            detectedPackaging = "std"
    declaredPackaging = frontmatter.get("metadata.packaging", "").lower()
    generatedBy = frontmatter.get("metadata.generated_by", "")
    if generatedBy == "skill-forge":
        if declaredPackaging not in {"cbd", "std"}:
            errors.append("Generated skill metadata.packaging must be CBD or STD.")
        else:
            if detectedPackaging is None:
                detectedPackaging = declaredPackaging
            elif detectedPackaging != declaredPackaging:
                errors.append("Detected packaging disagrees with metadata.packaging.")
            if not name.endswith(f"-{declaredPackaging}"):
                errors.append(f"Generated skill name must end in '-{declaredPackaging}'.")
    if expectedPackaging and not name.endswith(f"-{expectedPackaging}"):
        errors.append(f"Expected name suffix '-{expectedPackaging}'.")

    if detectedPackaging in {"cbd", "std"}:
        validateRuntimeContract(
            root,
            name,
            skillVersion,
            detectedPackaging,
            body,
            allowProposed,
            errors,
            warnings,
        )

    if detectedPackaging == "cbd":
        if not (root / "scripts" / "cbd_config.py").is_file():
            errors.append("CBD package is missing scripts/cbd_config.py.")
        lowered = body.lower()
        if "codebase-dependent" not in lowered or "autocab" not in lowered:
            errors.append("CBD SKILL.md does not document its AutoCAB codebase contract.")
    elif detectedPackaging == "std":
        if (root / "scripts" / "cbd_config.py").exists():
            errors.append("STD package must not contain scripts/cbd_config.py.")
        if (root / ".cursor" / "autoCAB").exists() or (root / ".agents" / "autoCAB").exists():
            errors.append("STD package must not embed a CBD config.")
        inspectPrivatePaths(root, errors)
        for bundleValue in re.findall(r"bundled as `([^`]+)`", body):
            bundlePath = root / bundleValue
            if not isInside(bundlePath, root) or not bundlePath.is_file():
                errors.append(f"STD declared bundled dependency is missing: {bundleValue}")

    if (
        detectedPackaging
        and not allowProposed
        and re.search(r"(?i)(pending approval|`proposed`|status:\s*proposed)", body)
    ):
        errors.append(
            "Package still contains proposed behavior awaiting approval "
            "(use --allowProposed only for staged review)."
        )

    inspectSymlinks(root, errors)
    inspectLinks(root, errors)
    inspectTemplateTokens(root, errors)

    if strict:
        for path in sorted(root.rglob("*"), key=lambda value: str(value)):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() in {".md", ".py", ".sh", ".json"}:
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if re.search(r"(?im)^\s*(?:TODO|TBA)\b", content):
                    errors.append(f"Strict mode found TODO/TBA in {path.relative_to(root)}.")

    return {
        "valid": not errors,
        "skill": name or None,
        "packaging": detectedPackaging,
        "skillMdLines": lineCount,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
    }


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Validate a generated Agent Skill package without executing it."
    )
    parser.add_argument("skillDir", help="Source skill directory (not a symlink).")
    parser.add_argument(
        "--expectedPackaging",
        choices=["cbd", "std"],
        help="Require matching lowercase mode suffix and invariants.",
    )
    parser.add_argument(
        "--allowProposed",
        action="store_true",
        help="Permit explicitly proposed steps in a staged, non-final package.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat TODO/TBA markers in text/code as errors.",
    )
    parser.add_argument("--output", help="Optional JSON report path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the package validation CLI."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parseArgs(argv)
    root = Path(args.skillDir).expanduser().resolve()
    report = validatePackage(root, args.expectedPackaging, args.strict, args.allowProposed)
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        logging.info("Wrote validation report: %s", output)
    print(rendered, end="")
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
